use crate::{App, model::Media};
use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use image::{
    DynamicImage, GenericImageView, ImageReader, Rgba, RgbaImage,
    codecs::jpeg::JpegEncoder,
    imageops::{self, FilterType},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{Cursor, Write},
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const MAX_IMAGE_BYTES: usize = 40 * 1024 * 1024;
const MAX_PIXELS: u64 = 50_000_000;

#[derive(Default)]
pub(crate) struct CoverState {
    jobs: Mutex<HashMap<String, CancellationToken>>,
    uploads: Mutex<HashMap<String, Instant>>,
}

impl App {
    pub fn store_cover_upload(&self, name: &str, image: &DynamicImage) -> Result<Value> {
        let mut uploads = self.cover_state.uploads.lock().unwrap();
        let directory = self.config.data_dir.join("assets");
        crate::reject_links(&directory)?;
        fs::create_dir_all(&directory)?;
        let id = Uuid::new_v4().to_string();
        let mut temporary = tempfile::NamedTempFile::new_in(&directory)?;
        image.write_to(&mut temporary, image::ImageFormat::Png)?;
        temporary.as_file().sync_all()?;
        temporary
            .persist_noclobber(directory.join(format!("{id}.png")))
            .map_err(|error| error.error)?;
        uploads.insert(id.clone(), Instant::now());
        Ok(
            json!({"id":id,"name":Path::new(name).file_name().unwrap_or_default().to_string_lossy(),"kind":"image"}),
        )
    }
}

fn release_upload(app: &App, id: &str) -> Result<()> {
    ensure!(
        Uuid::parse_str(id)?.to_string() == id,
        "Invalid cover asset ID"
    );
    let mut uploads = app.cover_state.uploads.lock().unwrap();
    if uploads.contains_key(id) {
        let path = app.config.data_dir.join("assets").join(format!("{id}.png"));
        crate::reject_links(&path)?;
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        uploads.remove(id);
    }
    Ok(())
}

pub(crate) fn clean_unused_assets(app: &App) -> Result<usize> {
    let mut uploads = app.cover_state.uploads.lock().unwrap();
    uploads.retain(|_, used| used.elapsed() < Duration::from_secs(24 * 60 * 60));
    let mut referenced = app
        .db
        .media()?
        .into_iter()
        .flat_map(|media| std::iter::once(media.path).chain(media.assets))
        .collect::<HashSet<_>>();
    for task in app.db.tasks_including_retained()? {
        referenced.extend(task.output_path);
        referenced.extend(task.previous_outputs);
        if let Some(publication) = task.publication {
            referenced.extend([publication.target, publication.temporary]);
            referenced.extend(publication.backup);
            for file in publication.auxiliary {
                referenced.extend([file.target, file.temporary]);
            }
        }
    }
    let directory = app.config.data_dir.join("assets");
    crate::reject_links(&directory)?;
    let mut removed = 0;
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let Some(id) = path.file_stem().and_then(|id| id.to_str()) else {
            continue;
        };
        if !entry.file_type()?.is_file()
            || !matches!(
                path.extension().and_then(|ext| ext.to_str()),
                Some("png" | "jpg")
            )
            || !Uuid::parse_str(id).is_ok_and(|uuid| uuid.to_string() == id)
            || uploads.contains_key(id)
            || referenced.contains(&path)
        {
            continue;
        }
        fs::remove_file(path)?;
        removed += 1;
    }
    Ok(removed)
}

#[derive(Clone, Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
struct Request {
    media_id: String,
    asset_id: String,
    original: bool,
    preset: String,
    fit_mode: String,
    focus_x: f64,
    focus_y: f64,
    quality: u8,
    background_color: String,
    prompt: String,
}
impl Default for Request {
    fn default() -> Self {
        Self {
            media_id: String::new(),
            asset_id: String::new(),
            original: false,
            preset: "landscape_16_9".into(),
            fit_mode: "crop".into(),
            focus_x: 0.5,
            focus_y: 0.5,
            quality: 90,
            background_color: "#000000".into(),
            prompt: String::new(),
        }
    }
}
impl Request {
    fn validate(&self) -> Result<(u32, u32)> {
        ensure!(
            Uuid::parse_str(&self.media_id)?.to_string() == self.media_id,
            "Invalid media ID"
        );
        ensure!(
            (1..=100).contains(&self.quality),
            "JPEG quality must be 1–100"
        );
        ensure!(
            matches!(self.fit_mode.as_str(), "crop" | "pad"),
            "Invalid cover fit mode"
        );
        ensure!(
            self.focus_x.is_finite()
                && self.focus_y.is_finite()
                && (0.0..=1.0).contains(&self.focus_x)
                && (0.0..=1.0).contains(&self.focus_y),
            "Cover focus must be 0–1"
        );
        color(&self.background_color)?;
        match self.preset.as_str() {
            "landscape_16_9" => Ok((1280, 720)),
            "portrait_9_16" => Ok((1080, 1920)),
            "portrait_3_4" => Ok((1080, 1440)),
            "landscape_4_3" => Ok((1440, 1080)),
            "square_1_1" => Ok((1080, 1080)),
            _ => bail!("Unknown cover preset"),
        }
    }
}

pub(crate) async fn command(app: &App, command: &str, args: Value) -> Result<Value> {
    let request: Request = serde_json::from_value(args)?;
    if command == "cover.release_upload" {
        release_upload(app, &request.asset_id)?;
        return Ok(json!(true));
    }
    if command == "cover.cancel" {
        if let Some(token) = app.cover_state.jobs.lock().unwrap().get(&request.media_id) {
            token.cancel();
        }
        return Ok(json!(true));
    }
    let dimensions = request.validate()?;
    let media = app.db.media_by_id(&request.media_id)?;
    let source = source_path(app, &media, &request)?;
    let bytes = read_image(&source)?;
    if command == "cover.generate" {
        ensure!(
            !request.prompt.trim().is_empty() && request.prompt.chars().count() <= 8000,
            "AI prompt must be 1–8000 characters"
        );
        let token = app.shutdown_token.child_token();
        {
            let mut jobs = app.cover_state.jobs.lock().unwrap();
            ensure!(
                !jobs.contains_key(&request.media_id),
                "Cover generation is already running"
            );
            jobs.insert(request.media_id.clone(), token.clone());
        }
        let result = generate(app, &request, bytes, &token).await;
        app.cover_state
            .jobs
            .lock()
            .unwrap()
            .remove(&request.media_id);
        let generated = result?;
        ensure!(!token.is_cancelled(), "Cover generation cancelled");
        let generated_image = decode(&generated)?;
        let png = png_bytes(&generated_image)?;
        let asset_id = save_asset(app, &media.id, "png", &png)?;
        return Ok(
            json!({"assetId":asset_id,"previewUrl":format!("data:image/png;base64,{}",STANDARD.encode(png)),"filename":"generated-cover.png"}),
        );
    }
    let edit = request.clone();
    let jpeg = tokio::task::spawn_blocking(move || {
        let image = decode(&bytes)?;
        let rendered = render(&image, &edit, dimensions)?;
        jpeg_bytes(&rendered, edit.quality)
    })
    .await??;
    match command {
        "cover.preview" => Ok(
            json!({"previewUrl":format!("data:image/jpeg;base64,{}",STANDARD.encode(&jpeg)),"width":dimensions.0,"height":dimensions.1}),
        ),
        "cover.export" => {
            let asset_id = save_asset(app, &media.id, "jpg", &jpeg)?;
            Ok(
                json!({"assetId":asset_id,"filename":format!("cover-{}.jpg",request.preset),"previewUrl":format!("data:image/jpeg;base64,{}",STANDARD.encode(&jpeg))}),
            )
        }
        _ => bail!("Unsupported cover command"),
    }
}

fn source_path(app: &App, media: &Media, request: &Request) -> Result<PathBuf> {
    let path = if !request.asset_id.is_empty() {
        ensure!(
            Uuid::parse_str(&request.asset_id)?.to_string() == request.asset_id,
            "Invalid cover asset ID"
        );
        if let Some(used) = app
            .cover_state
            .uploads
            .lock()
            .unwrap()
            .get_mut(&request.asset_id)
        {
            *used = Instant::now();
        }
        ["png", "jpg"]
            .into_iter()
            .map(|ext| {
                app.config
                    .data_dir
                    .join("assets")
                    .join(format!("{}.{}", request.asset_id, ext))
            })
            .find(|path| path.is_file())
            .context("Cover asset does not exist")?
    } else {
        let mut images = media.assets.iter().filter(|path| is_image(path));
        if request.original {
            images.next()
        } else {
            images.next_back()
        }
        .cloned()
        .context("This media has no original cover; upload an image first")?
    };
    let metadata = fs::symlink_metadata(&path)?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "Cover must be a regular owned file"
    );
    let canonical = path.canonicalize()?;
    let assets = app.config.data_dir.join("assets");
    let inside_assets = assets.is_dir() && canonical.starts_with(assets.canonicalize()?);
    ensure!(
        app.owns_download_path(&canonical) || inside_assets,
        "Cover path is outside owned storage"
    );
    Ok(canonical)
}
fn is_image(path: &Path) -> bool {
    path.extension().is_some_and(|ext| {
        matches!(
            ext.to_string_lossy().to_ascii_lowercase().as_str(),
            "jpg" | "jpeg" | "png" | "webp" | "gif" | "bmp"
        )
    })
}
fn read_image(path: &Path) -> Result<Vec<u8>> {
    ensure!(
        fs::metadata(path)?.len() <= MAX_IMAGE_BYTES as u64,
        "Cover is larger than 40 MiB"
    );
    let bytes = fs::read(path)?;
    ensure!(
        bytes.len() <= MAX_IMAGE_BYTES,
        "Cover is larger than 40 MiB"
    );
    Ok(bytes)
}
fn decode(bytes: &[u8]) -> Result<DynamicImage> {
    ensure!(
        !bytes.is_empty() && bytes.len() <= MAX_IMAGE_BYTES,
        "Invalid cover size"
    );
    let reader = ImageReader::new(Cursor::new(bytes)).with_guessed_format()?;
    let (width, height) = reader.into_dimensions()?;
    ensure!(
        width > 0
            && height > 0
            && width <= 16384
            && height <= 16384
            && u64::from(width) * u64::from(height) <= MAX_PIXELS,
        "Cover dimensions are too large"
    );
    let mut reader = ImageReader::new(Cursor::new(bytes)).with_guessed_format()?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(16384);
    limits.max_image_height = Some(16384);
    limits.max_alloc = Some(MAX_PIXELS * 8);
    reader.limits(limits);
    Ok(reader.decode()?)
}
fn color(value: &str) -> Result<Rgba<u8>> {
    ensure!(
        value.len() == 7
            && value.starts_with('#')
            && value[1..].chars().all(|c| c.is_ascii_hexdigit()),
        "Background color must be #RRGGBB"
    );
    Ok(Rgba([
        u8::from_str_radix(&value[1..3], 16)?,
        u8::from_str_radix(&value[3..5], 16)?,
        u8::from_str_radix(&value[5..7], 16)?,
        255,
    ]))
}
fn render(
    source: &DynamicImage,
    request: &Request,
    (width, height): (u32, u32),
) -> Result<DynamicImage> {
    let (sw, sh) = source.dimensions();
    let ratio = f64::from(width) / f64::from(height);
    let prepared = if request.fit_mode == "crop" {
        let (cw, ch) = if f64::from(sw) / f64::from(sh) > ratio {
            (
                (f64::from(sh) * ratio).round().max(1.0).min(f64::from(sw)) as u32,
                sh,
            )
        } else {
            (
                sw,
                (f64::from(sw) / ratio).round().max(1.0).min(f64::from(sh)) as u32,
            )
        };
        source
            .crop_imm(
                ((sw - cw) as f64 * request.focus_x).round() as u32,
                ((sh - ch) as f64 * request.focus_y).round() as u32,
                cw,
                ch,
            )
            .resize_exact(width, height, FilterType::Lanczos3)
    } else {
        source.resize(width, height, FilterType::Lanczos3)
    };
    let mut canvas = RgbaImage::from_pixel(width, height, color(&request.background_color)?);
    imageops::overlay(
        &mut canvas,
        &prepared.to_rgba8(),
        i64::from((width - prepared.width()) / 2),
        i64::from((height - prepared.height()) / 2),
    );
    Ok(DynamicImage::ImageRgb8(
        DynamicImage::ImageRgba8(canvas).to_rgb8(),
    ))
}
fn jpeg_bytes(image: &DynamicImage, quality: u8) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    JpegEncoder::new_with_quality(&mut bytes, quality).encode_image(image)?;
    Ok(bytes)
}
fn png_bytes(image: &DynamicImage) -> Result<Vec<u8>> {
    let mut bytes = Cursor::new(Vec::new());
    image.write_to(&mut bytes, image::ImageFormat::Png)?;
    Ok(bytes.into_inner())
}
fn save_asset(app: &App, media_id: &str, extension: &str, bytes: &[u8]) -> Result<String> {
    // Keep orphan cleanup from racing the file write and database reference.
    let _uploads = app.cover_state.uploads.lock().unwrap();
    let directory = app.config.data_dir.join("assets");
    fs::create_dir_all(&directory)?;
    ensure!(
        !fs::symlink_metadata(&directory)?.file_type().is_symlink(),
        "Asset directory must be owned"
    );
    let id = Uuid::new_v4().to_string();
    let target = directory.join(format!("{id}.{extension}"));
    let mut temporary = tempfile::NamedTempFile::new_in(&directory)?;
    temporary.write_all(bytes)?;
    temporary.as_file().sync_all()?;
    temporary
        .persist_noclobber(&target)
        .map_err(|error| error.error)?;
    #[cfg(unix)]
    fs::File::open(&directory)?.sync_all()?;
    let media = match app.db.append_media_asset(media_id, target.clone()) {
        Ok(media) => media,
        Err(error) => {
            fs::remove_file(target)?;
            return Err(error);
        }
    };
    app.emit("media.upsert", media.public());
    Ok(id)
}

fn endpoint(base: &str) -> Result<url::Url> {
    let mut url = url::Url::parse(if base.trim().is_empty() {
        "https://api.openai.com/v1"
    } else {
        base
    })?;
    ensure!(
        matches!(url.scheme(), "https" | "http")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none(),
        "Invalid AI endpoint"
    );
    let path = url.path().trim_end_matches('/');
    let path = if path.ends_with("/images/edits") {
        path.to_string()
    } else if path.is_empty() {
        "/v1/images/edits".into()
    } else {
        format!("{path}/images/edits")
    };
    url.set_path(&path);
    Ok(url)
}
pub(crate) async fn models(app: &App, args: &Value) -> Result<Value> {
    let settings = app.db.settings()?;
    let base = args
        .get("baseUrl")
        .map(|value| value.as_str().context("AI endpoint must be text"))
        .transpose()?
        .unwrap_or_else(|| settings["aiBaseUrl"].as_str().unwrap_or(""));
    ensure!(base.len() <= 8192, "AI endpoint is too long");
    let mut url = endpoint(base)?;
    let path = url
        .path()
        .strip_suffix("/images/edits")
        .context("Invalid AI endpoint")?;
    let path = format!("{path}/models");
    url.set_path(&path);
    let key = args
        .get("apiKey")
        .map(|value| value.as_str().context("AI API key must be text"))
        .transpose()?
        .filter(|key| !key.trim().is_empty())
        .map(str::to_owned)
        .or(app.credentials.secret("aiApiKey")?)
        .context("Configure an AI API key first")?;
    ensure!(
        !key.trim().is_empty() && key.len() <= 16384 && !key.chars().any(char::is_control),
        "Invalid AI API key"
    );
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(15))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let action = async {
        let response = client
            .get(url)
            .bearer_auth(key)
            .send()
            .await
            .map_err(|error| error.without_url())?;
        match response.status().as_u16() {
            401 | 403 => {
                bail!("Model list authentication failed; check the API key and its permissions")
            }
            404 | 405 => bail!(
                "This AI service does not support listing models; enter the model ID manually"
            ),
            status => ensure!(
                (200..300).contains(&status),
                "AI model service returned HTTP {status}"
            ),
        }
        let value: Value =
            serde_json::from_slice(&response_bytes(response, 2 * 1024 * 1024).await?)?;
        let records = value["data"]
            .as_array()
            .context("AI service returned an invalid model list")?;
        ensure!(
            records.len() <= 10000,
            "AI service returned too many models"
        );
        let mut ids = Vec::new();
        for record in records {
            let id = record["id"]
                .as_str()
                .context("AI service returned an invalid model ID")?;
            ensure!(
                !id.is_empty() && id.len() <= 256 && !id.chars().any(char::is_control),
                "AI service returned an invalid model ID"
            );
            ids.push(id);
        }
        ids.sort_unstable();
        ids.dedup();
        Ok(json!({"models":ids}))
    };
    tokio::select! { value=action=>value, _=app.shutdown_token.cancelled()=>bail!("Operation interrupted") }.context("Cannot fetch AI models")
}

async fn response_bytes(mut response: reqwest::Response, limit: usize) -> Result<Vec<u8>> {
    ensure!(
        response.content_length().is_none_or(|n| n <= limit as u64),
        "AI response is too large"
    );
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|error| error.without_url())?
    {
        ensure!(
            bytes.len() + chunk.len() <= limit,
            "AI response is too large"
        );
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}
async fn generate(
    app: &App,
    request: &Request,
    source: Vec<u8>,
    cancel: &CancellationToken,
) -> Result<Vec<u8>> {
    let key = app
        .credentials
        .secret("aiApiKey")?
        .filter(|key| !key.trim().is_empty())
        .context("Configure an AI API key first")?;
    let settings = app.db.settings()?;
    let endpoint = endpoint(settings["aiBaseUrl"].as_str().unwrap_or(""))?;
    let model = settings["aiModel"]
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or("gpt-image-2");
    let source = tokio::task::spawn_blocking(move || png_bytes(&decode(&source)?)).await??;
    let size = match request.preset.as_str() {
        "portrait_9_16" | "portrait_3_4" => "1024x1536",
        "square_1_1" => "1024x1024",
        _ => "1536x1024",
    };
    let form = reqwest::multipart::Form::new()
        .text("model", model.to_owned())
        .text("prompt", request.prompt.trim().to_owned())
        .text("size", size)
        .text("n", "1")
        .text("quality", "high")
        .text("input_fidelity", "high")
        .part(
            "image",
            reqwest::multipart::Part::bytes(source)
                .file_name("original-cover.png")
                .mime_str("image/png")?,
        );
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(300))
        .build()?;
    let action = async {
        let response = client
            .post(endpoint)
            .bearer_auth(&key)
            .multipart(form)
            .send()
            .await
            .map_err(|error| error.without_url())?;
        ensure!(
            response.status().is_success(),
            "AI image service returned HTTP {}",
            response.status().as_u16()
        );
        let document: Value = serde_json::from_slice(
            &response_bytes(response, MAX_IMAGE_BYTES * 4 / 3 + 1024 * 1024).await?,
        )?;
        let record = document["data"]
            .as_array()
            .and_then(|data| data.first())
            .context("AI service returned no image")?;
        if let Some(encoded) = record["b64_json"].as_str() {
            ensure!(
                encoded.len() <= MAX_IMAGE_BYTES * 4 / 3 + 4,
                "AI image is too large"
            );
            Ok(STANDARD.decode(encoded)?)
        } else if let Some(address) = record["url"].as_str() {
            let url = url::Url::parse(address)?;
            ensure!(
                matches!(url.scheme(), "http" | "https")
                    && url.username().is_empty()
                    && url.password().is_none(),
                "Invalid generated image URL"
            );
            let response = client
                .get(url)
                .send()
                .await
                .map_err(|error| error.without_url())?;
            ensure!(
                response.status().is_success(),
                "Generated image could not be downloaded"
            );
            response_bytes(response, MAX_IMAGE_BYTES).await
        } else {
            bail!("AI service returned no supported image data")
        }
    };
    tokio::select! { result=action=>result, _=cancel.cancelled()=>bail!("Cover generation cancelled") }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn uploads_release_and_orphan_cleanup_preserve_active_and_exported_images() -> Result<()>
    {
        let root = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let image = DynamicImage::new_rgb8(2, 2);
        let upload = app.store_cover_upload("upload.png", &image)?;
        let id = upload["id"].as_str().unwrap();
        let directory = app.config.data_dir.join("assets");
        let path = directory.join(format!("{id}.png"));
        let mut task = crate::tests::paused_task(HashMap::new());
        let previous = directory.join(format!("{}.png", Uuid::new_v4()));
        fs::write(&previous, b"preserve pending publication cleanup")?;
        task.previous_outputs.push(previous.clone());
        app.db.insert_task(&task)?;
        let main = directory.join(format!("{}.jpg", Uuid::new_v4()));
        image.save(&main)?;
        let media = Media {
            media_info: None,
            id: Uuid::new_v4().to_string(),
            task_id: task.id.clone(),
            title: "Cover upload".into(),
            filename: "video.mp4".into(),
            size: 1,
            duration: 1.0,
            path: main.clone(),
            assets: vec![],
        };
        app.db.complete(&task, &media)?;
        let export = app
            .command("cover.export", json!({"mediaId":media.id,"assetId":id}))
            .await?;
        let exported = directory.join(format!("{}.jpg", export["assetId"].as_str().unwrap()));
        let orphan = directory.join(format!("{}.png", Uuid::new_v4()));
        fs::write(&orphan, b"old abandoned upload")?;
        let report = crate::storage::cleanup(&app).await?;
        assert_eq!(report["unusedAssets"], 1);
        assert!(!orphan.exists());
        assert!(path.is_file() && exported.is_file() && main.is_file() && previous.is_file());
        app.command("cover.release_upload", json!({"assetId":id}))
            .await?;
        app.command("cover.release_upload", json!({"assetId":id}))
            .await?;
        app.command("cover.release_upload", json!({"assetId":export["assetId"]}))
            .await?;
        assert!(!path.exists());
        assert!(exported.is_file());
        assert!(
            app.command("cover.release_upload", json!({"assetId":"../video"}))
                .await
                .is_err()
        );
        let expired = app.store_cover_upload("expired.png", &image)?;
        let expired_id = expired["id"].as_str().unwrap();
        app.cover_state.uploads.lock().unwrap().insert(
            expired_id.into(),
            Instant::now() - Duration::from_secs(24 * 60 * 60),
        );
        assert_eq!(clean_unused_assets(&app)?, 1);
        assert!(!directory.join(format!("{expired_id}.png")).exists());
        let abandoned = app.store_cover_upload("crashed-editor.png", &image)?;
        let abandoned_path = directory.join(format!("{}.png", abandoned["id"].as_str().unwrap()));
        app.shutdown().await?;
        drop(app);
        let app = App::open(config).await?;
        assert_eq!(clean_unused_assets(&app)?, 1);
        assert!(!abandoned_path.exists());
        assert!(exported.is_file());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn model_discovery_uses_drafts_without_changing_saved_settings_and_handles_errors()
    -> Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        app.command("settings.save", json!({"settings":{"aiApiKey":"saved-fixture-key","aiModel":"keep-model","appearanceTheme":"light"}})).await?;
        let before = app.db.settings()?;
        for (status, body, declared, expected) in [
            (
                200,
                r#"{"data":[{"id":"z-image","secret":"never-return"},{"id":"a-image"},{"id":"z-image"}]}"#,
                None,
                "",
            ),
            (401, "{}", None, "authentication failed"),
            (404, "{}", None, "does not support listing models"),
            (200, r#"{"data":[{"id":42}]}"#, None, "invalid model ID"),
            (200, "{}", Some(2 * 1024 * 1024 + 1), "too large"),
        ] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
            let address = listener.local_addr()?;
            let server = tokio::spawn(async move {
                let (mut socket, _) = listener.accept().await?;
                let mut request = vec![0; 8192];
                let count = socket.read(&mut request).await?;
                let request = String::from_utf8_lossy(&request[..count]);
                ensure!(
                    request.starts_with("GET /custom/v1/models HTTP/1.1"),
                    "Wrong model endpoint"
                );
                ensure!(
                    request.to_ascii_lowercase().contains(if status == 200 {
                        "authorization: bearer saved-fixture-key"
                    } else {
                        "authorization: bearer draft-fixture-key"
                    }),
                    "Wrong model key source"
                );
                socket.write_all(format!("HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", declared.unwrap_or(body.len())).as_bytes()).await?;
                Ok::<_, anyhow::Error>(())
            });
            let mut args = json!({"baseUrl":format!("http://{address}/custom/v1/images/edits")});
            if status != 200 {
                args["apiKey"] = json!("draft-fixture-key");
            }
            let result = app.command("ai.models", args).await;
            if expected.is_empty() {
                assert_eq!(result?, json!({"models":["a-image","z-image"]}));
            } else {
                assert!(format!("{:#}", result.unwrap_err()).contains(expected));
            }
            server.await??;
            assert_eq!(app.db.settings()?, before);
        }
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn model_discovery_times_out_without_losing_the_saved_model() -> Result<()> {
        use tokio::io::AsyncReadExt;
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = [0; 8192];
            let _ = socket.read(&mut request).await;
            tokio::time::sleep(Duration::from_secs(30)).await;
        });
        let result = app
            .command(
                "ai.models",
                json!({"baseUrl":format!("http://{address}/v1"),"apiKey":"timeout-fixture"}),
            )
            .await;
        assert!(
            format!("{:#}", result.unwrap_err())
                .to_lowercase()
                .contains("timed out")
        );
        server.abort();
        assert_eq!(app.db.settings()?, json!({}));
        app.shutdown().await?;
        Ok(())
    }
    #[tokio::test]
    async fn audit_ai_completion_preserves_concurrent_export_and_publication() -> Result<()> {
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            sync::oneshot,
        };
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(HashMap::new());
        app.db.insert_task(&task)?;
        let png = png_bytes(&DynamicImage::new_rgb8(2, 2))?;
        let original = app.config.download_dir.join("original.png");
        fs::write(&original, &png)?;
        let media = Media {
            media_info: None,
            id: Uuid::new_v4().to_string(),
            task_id: task.id.clone(),
            title: "Audit".into(),
            filename: "video.mp4".into(),
            size: 1,
            duration: 1.0,
            path: app.config.download_dir.join("video.mp4"),
            assets: vec![original],
        };
        app.db.complete(&task, &media)?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let (started_tx, started) = oneshot::channel();
        let (release_tx, release) = oneshot::channel();
        let encoded = STANDARD.encode(&png);
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await?;
            let mut request = Vec::new();
            while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                ensure!(socket.read_buf(&mut request).await? > 0, "request ended");
            }
            let header_end = request
                .windows(4)
                .position(|part| part == b"\r\n\r\n")
                .unwrap()
                + 4;
            let length: usize = String::from_utf8_lossy(&request[..header_end])
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length:")
                        .map(|value| value.trim().to_owned())
                })
                .context("missing request size")?
                .parse()?;
            while request.len() < header_end + length {
                ensure!(
                    socket.read_buf(&mut request).await? > 0,
                    "request body ended"
                );
            }
            let _ = started_tx.send(());
            release.await?;
            let body = json!({"data":[{"b64_json":encoded}]}).to_string();
            socket.write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).as_bytes()).await?;
            Ok::<_, anyhow::Error>(())
        });
        app.command("settings.save",json!({"settings":{"aiApiKey":"fake audit key","aiBaseUrl":format!("http://{address}/v1")}})).await?;
        let generating = app.clone();
        let media_id = media.id.clone();
        let generation = tokio::spawn(async move {
            generating
                .command(
                    "cover.generate",
                    json!({"mediaId":media_id,"original":true,"prompt":"Audit"}),
                )
                .await
        });
        tokio::time::timeout(Duration::from_secs(10), started).await??;
        let export = app
            .command("cover.export", json!({"mediaId":media.id,"original":true}))
            .await?;
        let mut published = app.db.media_by_id(&media.id)?;
        published.size = 999;
        app.db.complete(&task, &published)?;
        let _ = release_tx.send(());
        tokio::time::timeout(Duration::from_secs(10), generation).await???;
        server.await??;
        let result = app.db.media_by_id(&media.id)?;
        assert_eq!(result.assets.len(), 3);
        assert_eq!(result.size, 999);
        assert!(
            result
                .assets
                .iter()
                .any(|path| path.file_stem().unwrap() == export["assetId"].as_str().unwrap())
        );
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn crops_focus_and_preserves_padding_background() -> Result<()> {
        let source = DynamicImage::ImageRgb8(image::RgbImage::from_fn(200, 100, |x, _| {
            if x < 100 {
                image::Rgb([255, 0, 0])
            } else {
                image::Rgb([0, 0, 255])
            }
        }));
        let mut request = Request {
            preset: "square_1_1".into(),
            focus_x: 0.0,
            ..Default::default()
        };
        let left = render(&source, &request, (100, 100))?.to_rgb8();
        assert_eq!(left.get_pixel(50, 50).0, [255, 0, 0]);
        request.focus_x = 1.0;
        let right = render(&source, &request, (100, 100))?.to_rgb8();
        assert_eq!(right.get_pixel(50, 50).0, [0, 0, 255]);
        request.fit_mode = "pad".into();
        request.background_color = "#00ff00".into();
        let padded = render(&source, &request, (100, 100))?.to_rgb8();
        assert_eq!(padded.get_pixel(50, 0).0, [0, 255, 0]);
        assert!(jpeg_bytes(&DynamicImage::ImageRgb8(padded), 90)?.starts_with(&[0xff, 0xd8]));
        Ok(())
    }
    #[test]
    fn rejects_invalid_images_and_edit_arguments() {
        assert!(decode(b"not an image").is_err());
        assert!(color("#💜💜").is_err());
        assert!(
            Request {
                media_id: Uuid::new_v4().to_string(),
                focus_x: f64::NAN,
                ..Default::default()
            }
            .validate()
            .is_err()
        );
        assert!(endpoint("https://secret@example.com/v1").is_err());
        assert!(endpoint("https://example.com/v1?key=secret").is_err());
        assert_eq!(
            endpoint("https://example.com/v1").unwrap().path(),
            "/v1/images/edits"
        );
    }
}

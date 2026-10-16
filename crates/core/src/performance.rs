//! Opt-in measurements against the real core paths and synthetic, isolated data.
//!
//! Run in release mode with `HUIFA_PERF=1` and optionally `HUIFA_TEST_TOOLS`:
//! `cargo test --locked --release -p huifa-core performance_audit_baseline -- --ignored --nocapture --test-threads=1`
//! Fixture creation and the first tool freeze are outside the measured samples.

use crate::{
    App, AppConfig, DownloadOptions,
    db::Database,
    model::{Media, Task},
    tools,
};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    future::Future,
    hint::black_box,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

const DATABASE_SAMPLES: usize = 21;
const IO_SAMPLES: usize = 7;
const PROCESS_SAMPLES: usize = 5;

fn media_id(index: usize) -> String {
    format!("00000000-0000-4000-8000-{index:012}")
}

fn task_id(index: usize) -> String {
    format!("10000000-0000-4000-8000-{index:012}")
}

fn fixture_media(root: &Path, index: usize) -> Media {
    Media {
        id: media_id(index),
        task_id: task_id(index),
        title: format!("Performance audit synthetic media {index:05}"),
        filename: format!("media-{index:05}.mp4"),
        size: 128 * 1024 * 1024 + index as u64,
        duration: 300.0 + index as f64 / 10.0,
        path: root.join("output").join(format!("media-{index:05}.mp4")),
        assets: vec![
            root.join("output").join(format!("media-{index:05}.jpg")),
            root.join("output")
                .join(format!("media-{index:05}.info.json")),
        ],
    }
}

fn seed_catalog(root: &Path, count: usize) -> Result<(PathBuf, usize)> {
    let data = root.join("data");
    std::fs::create_dir_all(&data)?;
    let path = data.join("rust-core.sqlite3");
    // Create the production schema, then seed using a separate transaction.
    drop(Database::open(&path)?);
    let mut connection = Connection::open(&path)?;
    let transaction = connection.transaction()?;
    let mut record_bytes = 0;
    {
        let mut insert =
            transaction.prepare("INSERT INTO media(id,task_id,record) VALUES(?1,?2,?3)")?;
        for index in 0..count {
            let media = fixture_media(root, index);
            let record = serde_json::to_string(&media)?;
            record_bytes += record.len();
            insert.execute(params![media.id, media.task_id, record])?;
        }
    }
    transaction.commit()?;
    drop(connection);
    Ok((path, record_bytes))
}

fn summary(name: &str, warmups: usize, mut samples: Vec<f64>, details: Value) -> Value {
    let original = samples.clone();
    samples.sort_by(f64::total_cmp);
    let p95_index = (samples.len() * 95).div_ceil(100).saturating_sub(1);
    json!({
        "name": name,
        "warmups": warmups,
        "samples": samples.len(),
        "median_ms": samples[samples.len() / 2],
        "p95_ms": samples[p95_index],
        "min_ms": samples[0],
        "max_ms": samples[samples.len() - 1],
        "samples_ms": original,
        "details": details,
    })
}

fn measure_sync<T>(
    name: &str,
    warmups: usize,
    samples: usize,
    details: Value,
    mut operation: impl FnMut() -> Result<T>,
) -> Result<Value> {
    for _ in 0..warmups {
        black_box(operation()?);
    }
    let mut elapsed = Vec::with_capacity(samples);
    for _ in 0..samples {
        let start = Instant::now();
        let output = operation()?;
        elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
        black_box(output);
    }
    Ok(summary(name, warmups, elapsed, details))
}

async fn measure_async<T, F, Fut>(
    name: &str,
    warmups: usize,
    samples: usize,
    details: Value,
    mut operation: F,
) -> Result<Value>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T>>,
{
    for _ in 0..warmups {
        black_box(operation().await?);
    }
    let mut elapsed = Vec::with_capacity(samples);
    for _ in 0..samples {
        let start = Instant::now();
        let output = operation().await?;
        elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
        black_box(output);
    }
    Ok(summary(name, warmups, elapsed, details))
}

fn measure_database(root: &Path, count: usize, results: &mut Vec<Value>) -> Result<PathBuf> {
    let (path, record_bytes) = seed_catalog(root, count)?;
    let database = Database::open(&path)?;
    let index = count / 2;
    let expected = serde_json::to_value(fixture_media(root, index))?;
    let id = media_id(index);
    let task = task_id(index);
    let missing_id = media_id(count + 1);
    let missing_task = task_id(count + 1);
    ensure!(serde_json::to_value(database.media_by_id(&id)?)? == expected);
    ensure!(
        serde_json::to_value(
            database
                .media_for_task(&task)?
                .context("Fixture media is missing")?
        )? == expected
    );
    ensure!(database.media_for_task(&missing_task)?.is_none());
    ensure!(database.media_by_id(&missing_id).unwrap_err().to_string() == "Media does not exist");
    let details = json!({
        "catalog_records": count,
        "record_bytes_total": record_bytes,
        "hit_index": index,
        "query_operations_per_sample": 1,
    });
    results.push(measure_sync(
        "database.media_by_id.hit",
        2,
        DATABASE_SAMPLES,
        details.clone(),
        || database.media_by_id(&id),
    )?);
    results.push(measure_sync(
        "database.media_for_task.hit",
        2,
        DATABASE_SAMPLES,
        details.clone(),
        || database.media_for_task(&task),
    )?);
    results.push(measure_sync(
        "database.media_by_id.miss",
        2,
        DATABASE_SAMPLES,
        details.clone(),
        || {
            let result = database.media_by_id(&missing_id);
            ensure!(result.is_err(), "A missing fixture ID unexpectedly exists");
            Ok(result)
        },
    )?);
    results.push(measure_sync(
        "database.media_for_task.miss",
        2,
        DATABASE_SAMPLES,
        details,
        || database.media_for_task(&missing_task),
    )?);
    drop(database);
    Ok(path)
}

fn fixture_task(tools: HashMap<String, PathBuf>) -> Task {
    Task {
        id: task_id(0),
        url: "https://example.test/performance-fixture.mp4".into(),
        title: "Performance audit tool fixture".into(),
        status: "paused".into(),
        stage: "queued".into(),
        progress: 0.0,
        stage_progress: None,
        stage_indeterminate: true,
        speed: None,
        eta: None,
        error: None,
        created_at: "2026-01-01T00:00:00Z".into(),
        options: DownloadOptions::default(),
        output_path: None,
        cookie_snapshot: None,
        proxy_snapshot: None,
        manifest: None,
        publication: None,
        conversion: false,
        tools,
    }
}

fn available_tools(value: &Value) -> bool {
    value.as_array().is_some_and(|tools| {
        tools.len() == 4
            && tools
                .iter()
                .all(|tool| tool["available"].as_bool() == Some(true))
    })
}

async fn measure_scheduler_delay(app: Arc<App>, task: Task) -> Result<Value> {
    // A separate single-worker runtime makes blocking observable without relying
    // on how many spare workers happen to be available in this test's runtime.
    tokio::task::spawn_blocking(move || -> Result<Value> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        runtime.block_on(async move {
            let period = Duration::from_millis(5);
            let mut ticker = tokio::time::interval(period);
            ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let (stop, mut stopped) = tokio::sync::oneshot::channel::<()>();
            let heartbeat = tokio::spawn(async move {
                let mut delays = Vec::new();
                loop {
                    tokio::select! {
                        biased;
                        deadline = ticker.tick() => delays.push(
                            tokio::time::Instant::now().saturating_duration_since(deadline)
                                .as_secs_f64() * 1000.0
                        ),
                        _ = &mut stopped => break,
                    }
                }
                delays
            });
            tokio::time::sleep(period * 2).await;
            for _ in 0..IO_SAMPLES {
                black_box(app.task_tool(&task, "ffprobe").await?);
                // Let the overdue ticker run and establish its next deadline.
                tokio::time::sleep(period * 2).await;
            }
            let _ = stop.send(());
            let delays = heartbeat.await.context("Performance heartbeat stopped")?;
            ensure!(
                !delays.is_empty(),
                "Performance heartbeat recorded no samples"
            );
            Ok(summary(
                "scheduler.task_tool_ffprobe.current_thread",
                0,
                delays,
                json!({
                    "heartbeat_period_ms": 5,
                    "tool_resolutions": IO_SAMPLES,
                    "runtime_worker_threads": 1,
                    "measurement": "heartbeat lateness relative to its scheduled deadline",
                }),
            ))
        })
    })
    .await
    .context("Performance scheduler worker stopped")?
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "opt-in release performance audit; requires HUIFA_PERF=1 and installed native runtime tools"]
async fn performance_audit_baseline() -> Result<()> {
    // `--include-ignored` in ordinary CI must remain inexpensive.
    if std::env::var("HUIFA_PERF").ok().as_deref() != Some("1") {
        eprintln!("Performance audit skipped: set HUIFA_PERF=1 to measure isolated fixtures");
        return Ok(());
    }
    ensure!(
        !cfg!(debug_assertions),
        "Run the performance audit with --release"
    );
    let tool_directory = std::env::var_os("HUIFA_TEST_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools"))
        .canonicalize()
        .context("Install local runtime tools before the opt-in performance audit")?;
    let mut runtime_sizes = serde_json::Map::new();
    for name in ["yt-dlp", "ffmpeg", "ffprobe", "deno"] {
        let path = tool_directory.join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
        let metadata = std::fs::metadata(&path)
            .with_context(|| format!("The performance audit requires local {name}"))?;
        ensure!(
            metadata.is_file(),
            "The performance audit requires a regular runtime executable"
        );
        runtime_sizes.insert(name.into(), json!(metadata.len()));
    }
    let root = tempfile::tempdir()?;
    let mut results = Vec::new();
    measure_database(&root.path().join("catalog-1000"), 1000, &mut results)?;
    let large_root = root.path().join("catalog-10000");
    let database = measure_database(&large_root, 10000, &mut results)?;
    ensure!(database.parent() == Some(large_root.join("data").as_path()));
    let app = App::open(AppConfig {
        data_dir: large_root.join("data"),
        download_dir: large_root.join("output"),
        tool_dir: Some(tool_directory),
    })
    .await?;

    let paths = tools::freeze(&app)?;
    let task = fixture_task(paths.clone());
    results.push(measure_sync(
        "tools.freeze_existing",
        1,
        IO_SAMPLES,
        json!({
            "runtime_bytes": runtime_sizes.values().filter_map(Value::as_u64).sum::<u64>(),
            "first_freeze_excluded": true,
        }),
        || tools::freeze(&app),
    )?);
    for name in ["ffmpeg", "ffprobe", "deno"] {
        ensure!(app.task_tool(&task, name).await? == paths[name]);
        results.push(
            measure_async(
                &format!("tools.task_tool.{name}"),
                1,
                IO_SAMPLES,
                json!({
                    "runtime_bytes": runtime_sizes[name],
                }),
                || app.task_tool(&task, name),
            )
            .await?,
        );
    }
    results.push(measure_scheduler_delay(Arc::clone(&app), task).await?);
    let checked = tools::check(&app).await?;
    ensure!(
        available_tools(&checked),
        "Every local runtime must pass its version check"
    );
    results.push(
        measure_async(
            "tools.check",
            1,
            PROCESS_SAMPLES,
            json!({
                "runtime_processes_per_sample": 4,
            }),
            || tools::check(&app),
        )
        .await?,
    );
    let snapshot = app.command("snapshot", json!({})).await?;
    ensure!(
        snapshot["media"]
            .as_array()
            .is_some_and(|media| media.len() == 10000)
    );
    ensure!(snapshot["tasks"].as_array().is_some_and(Vec::is_empty));
    ensure!(available_tools(&snapshot["tools"]));
    results.push(
        measure_async(
            "core.snapshot",
            1,
            PROCESS_SAMPLES,
            json!({
                "catalog_records": 10000,
                "task_records": 0,
                "includes_tools_check": true,
                "excludes_host_transport_serialization": true,
            }),
            || app.command("snapshot", json!({})),
        )
        .await?,
    );
    app.shutdown().await?;
    println!(
        "PERF_JSON {}",
        serde_json::to_string(&json!({
            "schema_version": 1,
            "package_version": env!("CARGO_PKG_VERSION"),
            "os": std::env::consts::OS,
            "architecture": std::env::consts::ARCH,
            "profile": "release",
            "scope": "isolated synthetic catalog and local official runtime tools; warmed OS file cache",
            "percentile_method": "nearest rank; p95 is the maximum for fewer than 20 samples",
            "runtime_bytes": runtime_sizes,
            "measurements": results,
        }))?
    );
    Ok(())
}

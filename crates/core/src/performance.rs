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
        media_info: None,
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
        kind: "media".into(),
        files: vec![],
        id: task_id(0),
        url: "https://example.test/performance-fixture.mp4".into(),
        title: "Performance audit tool fixture".into(),
        thumbnail: None,
        size: None,
        transfer_bytes: vec![],
        source_info: None,
        media_info: None,
        steps: vec![],
        step_index: None,
        status: "paused".into(),
        stage: "queued".into(),
        progress: 0.0,
        stage_progress: None,
        stage_indeterminate: true,
        speed: None,
        eta: None,
        error: None,
        warnings: vec![],
        created_at: "2026-01-01T00:00:00Z".into(),
        completed_at: None,
        options: DownloadOptions::default(),
        download_dir: None,
        output_path: None,
        previous_outputs: vec![],
        cookie_snapshot: None,
        proxy_snapshot: None,
        manifest: None,
        publication: None,
        destination: None,
        priority: 0,
        live: false,
        conversion: false,
        compression: None,
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

#[cfg(windows)]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires HUIFA_GPU_CONCURRENCY=1 and an eligible NVIDIA Windows host"]
async fn real_gpu_jobs_overlap_and_release_processing_resources() -> Result<()> {
    use tokio_util::sync::CancellationToken;
    if std::env::var("HUIFA_GPU_CONCURRENCY").as_deref() != Ok("1") {
        return Ok(());
    }
    let root = tempfile::tempdir()?;
    let app = App::open(AppConfig {
        data_dir: root.path().join("data"),
        download_dir: root.path().join("downloads"),
        tool_dir: Some(
            std::env::var_os("HUIFA_TEST_TOOLS")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools")
                }),
        ),
    })
    .await?;
    let token = CancellationToken::new();
    let source = app.download_directory().join("parallel-source-one.mkv");
    let args: Vec<String> = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=3840x2160:rate=30",
        "-t",
        "40",
        "-an",
        "-threads",
        "2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-b:v",
        "20M",
        "-pix_fmt",
        "yuv420p",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
    ]
    .into_iter()
    .map(str::to_string)
    .chain([source.to_string_lossy().into_owned()])
    .collect();
    crate::process::run(&app.tool("ffmpeg")?, &args, None, &token, None).await?;
    let source_hash = crate::download::hash_file(&source)?;
    let second = app.download_directory().join("parallel-source-two.mkv");
    std::fs::hard_link(&source, &second)?;
    let mut originals = Vec::new();
    for (index, source) in [source, second].into_iter().enumerate() {
        let mut task = fixture_task(Default::default());
        task.id = task_id(index);
        task.title = format!("Parallel GPU source {}", index + 1);
        task.stage("completed", "completed", Some(100.0));
        task.output_path = Some(source.clone());
        task.download_dir = Some(app.download_directory());
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &Media {
                id: media_id(index),
                task_id: task.id.clone(),
                title: task.title.clone(),
                filename: source.file_name().unwrap().to_string_lossy().into_owned(),
                size: source_hash.1,
                duration: 40.0,
                path: source,
                assets: vec![],
                media_info: None,
            },
        )?;
        originals.push(task.id);
    }
    let started = Instant::now();
    let mut ids = Vec::new();
    for original in &originals {
        let queued = app.command("tasks.convert", json!({"id":original,"compression":{"videoBitrateBps":4_000_000,"encoder":"hevc_nvenc"}})).await?;
        ids.push(queued["id"].as_str().unwrap().to_string());
    }
    let deadline = Instant::now() + Duration::from_secs(300);
    let mut samples = Vec::new();
    let mut maximum_overlap = Duration::ZERO;
    let mut overlap_started = None;
    loop {
        let tasks: Vec<_> = ids
            .iter()
            .map(|id| app.db.task(id))
            .collect::<Result<_>>()?;
        for task in &tasks {
            ensure!(
                !matches!(
                    task.status.as_str(),
                    "failed" | "processing_failed" | "cancelled"
                ),
                "GPU task failed: {} {:?}",
                task.stage,
                task.error
            );
        }
        let encoders = tasks
            .iter()
            .filter(|task| {
                task.stage == "compression_encoding"
                    && task.stage_progress.is_some_and(|progress| progress > 0.0)
            })
            .count();
        let children = app_cycle_windows::children()?;
        let active_children = children
            .iter()
            .filter(|(_, name)| name.eq_ignore_ascii_case("ffmpeg.exe"))
            .count();
        if encoders == 2 && active_children >= 2 {
            let since = *overlap_started.get_or_insert_with(Instant::now);
            maximum_overlap = maximum_overlap.max(since.elapsed());
        } else {
            overlap_started = None;
        }
        samples.push(json!({"elapsed_ms":started.elapsed().as_millis(),"stages":tasks.iter().map(|task| (&task.stage,task.stage_progress)).collect::<Vec<_>>(),"ffmpeg_children":active_children}));
        if tasks.iter().all(|task| task.status == "completed") {
            break;
        }
        if Instant::now() >= deadline {
            for id in &ids {
                let _ = app.command("tasks.cancel", json!({"id":id})).await;
            }
            anyhow::bail!("Parallel GPU acceptance exceeded five minutes");
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let elapsed = started.elapsed();
    let outputs: Vec<_> = ids
        .iter()
        .map(|id| {
            app.db
                .media_for_task(id)?
                .context("Completed GPU output is missing")
        })
        .collect::<Result<_>>()?;
    let source_unchanged = originals.iter().all(|id| {
        app.db
            .media_for_task(id)
            .ok()
            .flatten()
            .is_some_and(|media| {
                crate::download::hash_file(&media.path).is_ok_and(|hash| hash == source_hash)
            })
    });
    app.shutdown().await?;
    ensure!(source_unchanged, "Parallel GPU changed an input");
    let record = json!({"platform":"windows","profile":"debug","tasks":2,"encoder":"hevc_nvenc","source":"40s 3840x2160 30fps 8bit SDR H264, video only","elapsed_ms":elapsed.as_millis(),"maximum_continuous_overlap_ms":maximum_overlap.as_millis(),"source_unchanged":source_unchanged,"output_bytes":outputs.iter().map(|media|media.size).collect::<Vec<_>>(),"all_owned_children_exited":app_cycle_windows::children()?.is_empty(),"samples":samples});
    let output =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../output/performance-20261020");
    std::fs::create_dir_all(&output)?;
    std::fs::write(
        output.join("gpu-concurrency.json"),
        serde_json::to_vec_pretty(&record)?,
    )?;
    ensure!(
        maximum_overlap >= Duration::from_millis(500),
        "GPU encodes did not overlap for 500 ms; check free RAM/VRAM and admission results"
    );
    ensure!(
        record["all_owned_children_exited"] == true,
        "GPU acceptance left owned children"
    );
    Ok(())
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
    let mut runtime_payload_bytes = 0;
    let javascript = if tool_directory
        .join(format!("deno{}", std::env::consts::EXE_SUFFIX))
        .is_file()
    {
        "deno"
    } else {
        "qjs"
    };
    for name in ["yt-dlp", "ffmpeg", "ffprobe", javascript] {
        let path = tool_directory.join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
        let metadata = std::fs::metadata(&path)
            .with_context(|| format!("The performance audit requires local {name}"))?;
        ensure!(
            metadata.is_file(),
            "The performance audit requires a regular runtime executable"
        );
        let mut size = metadata.len();
        runtime_payload_bytes += metadata.len();
        if cfg!(windows) && matches!(name, "ffmpeg" | "ffprobe") {
            for entry in std::fs::read_dir(&tool_directory)? {
                let entry = entry?;
                if entry.file_name().to_string_lossy().ends_with(".dll") {
                    let bytes = entry.metadata()?.len();
                    size += bytes;
                    if name == "ffmpeg" {
                        runtime_payload_bytes += bytes;
                    }
                }
            }
        }
        runtime_sizes.insert(name.into(), json!(size));
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
            "runtime_bytes": runtime_payload_bytes,
            "first_freeze_excluded": true,
        }),
        || tools::freeze(&app),
    )?);
    for name in ["ffmpeg", "ffprobe", javascript] {
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

#[cfg(windows)]
mod app_cycle_windows {
    use super::*;
    use std::ffi::c_void;
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::Threading::{GetCurrentProcess, GetProcessHandleCount},
    };

    #[repr(C)]
    #[derive(Default)]
    struct Memory {
        cb: u32,
        faults: u32,
        peak_working_set: usize,
        working_set: usize,
        peak_paged: usize,
        paged: usize,
        peak_nonpaged: usize,
        nonpaged: usize,
        pagefile: usize,
        peak_pagefile: usize,
        private: usize,
    }
    #[repr(C)]
    struct ProcessEntry {
        size: u32,
        usage: u32,
        id: u32,
        heap: usize,
        module: u32,
        threads: u32,
        parent: u32,
        priority: i32,
        flags: u32,
        name: [u16; 260],
    }
    #[link(name = "psapi")]
    unsafe extern "system" {
        fn GetProcessMemoryInfo(process: *mut c_void, counters: *mut Memory, size: u32) -> i32;
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateToolhelp32Snapshot(flags: u32, process: u32) -> *mut c_void;
        fn Process32FirstW(snapshot: *mut c_void, entry: *mut ProcessEntry) -> i32;
        fn Process32NextW(snapshot: *mut c_void, entry: *mut ProcessEntry) -> i32;
    }

    pub(super) fn processes() -> Result<Vec<(u32, u32, String, u32)>> {
        unsafe {
            let list = CreateToolhelp32Snapshot(2, 0);
            ensure!(list as isize != -1, "Cannot enumerate process fixtures");
            let mut entry: ProcessEntry = std::mem::zeroed();
            entry.size = std::mem::size_of::<ProcessEntry>() as u32;
            let mut processes = Vec::new();
            if Process32FirstW(list, &mut entry) != 0 {
                loop {
                    let length = entry
                        .name
                        .iter()
                        .position(|character| *character == 0)
                        .unwrap_or(entry.name.len());
                    processes.push((
                        entry.id,
                        entry.parent,
                        String::from_utf16_lossy(&entry.name[..length]),
                        entry.threads,
                    ));
                    if Process32NextW(list, &mut entry) == 0 {
                        break;
                    }
                }
            }
            CloseHandle(list);
            Ok(processes)
        }
    }

    pub(super) fn children() -> Result<Vec<(u32, String)>> {
        let processes = processes()?;
        let mut parents = vec![std::process::id()];
        let mut children = Vec::new();
        while let Some(parent) = parents.pop() {
            for (id, _, name, _) in processes.iter().filter(|(_, owner, _, _)| *owner == parent) {
                parents.push(*id);
                children.push((*id, name.clone()));
            }
        }
        Ok(children)
    }

    pub(super) fn snapshot() -> Result<Value> {
        let mut memory = Memory {
            cb: std::mem::size_of::<Memory>() as u32,
            ..Default::default()
        };
        let mut handles = 0;
        unsafe {
            ensure!(
                GetProcessMemoryInfo(
                    GetCurrentProcess(),
                    &mut memory,
                    std::mem::size_of::<Memory>() as u32
                ) != 0,
                "Cannot measure process memory"
            );
            ensure!(
                GetProcessHandleCount(GetCurrentProcess(), &mut handles) != 0,
                "Cannot measure process handles"
            );
        }
        let threads = processes()?
            .into_iter()
            .find(|(id, _, _, _)| *id == std::process::id())
            .map(|(_, _, _, threads)| threads);
        Ok(
            json!({"private_bytes":memory.private,"working_set_bytes":memory.working_set,"handles":handles,"threads":threads}),
        )
    }
}

#[cfg(windows)]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "opt-in real CPU compression lifecycle measurements; requires HUIFA_APP_RESOURCE_CYCLES=1 and local tools"]
async fn app_resource_cycles() -> Result<()> {
    use sha2::{Digest, Sha256};
    use tokio_util::sync::CancellationToken;
    if std::env::var("HUIFA_APP_RESOURCE_CYCLES").as_deref() != Ok("1") {
        return Ok(());
    }
    let directory = std::env::var_os("HUIFA_TEST_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools"))
        .canonicalize()?;
    let root = tempfile::tempdir()?;
    let app = App::open(AppConfig {
        data_dir: root.path().join("data"),
        download_dir: root.path().join("downloads"),
        tool_dir: Some(directory),
    })
    .await?;
    app.command("settings.save", json!({"settings":{"analysisThreads":2}}))
        .await?;
    let source = app.download_directory().join("resource-cycle-source.mkv");
    let args: Vec<String> = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=8",
        "-t",
        "2",
        "-an",
        "-threads",
        "2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-b:v",
        "2M",
    ]
    .into_iter()
    .map(str::to_string)
    .chain([source.to_string_lossy().into_owned()])
    .collect();
    crate::process::run(
        &app.tool("ffmpeg")?,
        &args,
        None,
        &CancellationToken::new(),
        None,
    )
    .await?;
    let source_hash = hex::encode(Sha256::digest(std::fs::read(&source)?));
    let source_bytes = std::fs::metadata(&source)?.len();
    let source_path = app.owned_path(&source)?;
    let mut source_task = fixture_task(HashMap::new());
    source_task.stage("completed", "completed", Some(100.0));
    source_task.title = "Resource cycle source".into();
    source_task.output_path = Some(source_path.clone());
    source_task.download_dir = Some(app.download_directory());
    app.db.insert_task(&source_task)?;
    app.db.complete(
        &source_task,
        &Media {
            id: media_id(0),
            task_id: source_task.id.clone(),
            title: source_task.title.clone(),
            filename: "resource-cycle-source.mkv".into(),
            size: source_bytes,
            duration: 2.0,
            path: source_path,
            assets: vec![],
            media_info: None,
        },
    )?;
    let mut history_payload = Value::Null;
    let mut samples = Vec::new();
    let mut warmup = Vec::new();
    let mut baseline = Value::Null;
    let mut completed_count = 0;
    let mut cancelled_count = 0;
    let mut retry_count = 0;
    let warmup_rounds = 6;
    for round in 0..warmup_rounds + 30 {
        let kind = ["complete", "cancel", "cancel_retry_complete"][round % 3];
        let started = Instant::now();
        let queued = app.command("tasks.convert", json!({"id":source_task.id,"compression":{"videoBitrateBps":100000,"encoder":"libx265"}})).await?;
        let id = queued["id"]
            .as_str()
            .context("Compression did not queue a task")?
            .to_string();
        let mut cancellation_stage = None;
        if kind != "complete" {
            let deadline = Instant::now() + Duration::from_secs(30);
            loop {
                ensure!(
                    Instant::now() < deadline,
                    "Timed out waiting for an actual processing child"
                );
                let current = app.db.task(&id)?;
                ensure!(
                    !matches!(
                        current.status.as_str(),
                        "completed" | "failed" | "processing_failed"
                    ),
                    "Task finished before cancellation: {} / {} {:?}",
                    current.status,
                    current.stage,
                    current.error
                );
                let children = app_cycle_windows::children()?;
                if matches!(
                    current.stage.as_str(),
                    "compression_scanning" | "compression_encoding"
                ) && children.iter().any(|(_, name)| {
                    matches!(
                        name.to_ascii_lowercase().as_str(),
                        "ffmpeg.exe" | "ffprobe.exe"
                    )
                }) {
                    cancellation_stage = Some(current.stage);
                    app.command("tasks.cancel", json!({"id":id})).await?;
                    ensure!(
                        app.db.task(&id)?.status == "cancelled",
                        "Processing cancellation did not complete"
                    );
                    cancelled_count += 1;
                    break;
                }
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
            if kind == "cancel_retry_complete" {
                app.command("tasks.retry", json!({"id":id})).await?;
                retry_count += 1;
            }
        }
        let mut output_bytes = None;
        if kind != "cancel" {
            tokio::time::timeout(Duration::from_secs(60), async {
                loop {
                    let current = app.db.task(&id)?;
                    ensure!(
                        !matches!(current.status.as_str(), "failed" | "processing_failed"),
                        "CPU compression failed at {}: {:?}",
                        current.stage,
                        current.error
                    );
                    if current.status == "completed" {
                        let output = app
                            .db
                            .media_for_task(&id)?
                            .context("Compression published no media")?;
                        ensure!(
                            output.path.is_file() && output.size > 0,
                            "Compression published no output bytes"
                        );
                        ensure!(
                            output
                                .media_info
                                .as_ref()
                                .is_some_and(|info| info.width == Some(320)
                                    && info.height == Some(180)
                                    && info.video_codec.as_deref() == Some("hevc")),
                            "Output media properties were not verified"
                        );
                        output_bytes = Some(output.size);
                        history_payload = current.public();
                        return Ok::<_, anyhow::Error>(());
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await??;
            completed_count += 1;
        }
        app.command("tasks.delete", json!({"id":id,"deleteFiles":true}))
            .await?;
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if app.runners.lock().unwrap().is_empty()
                    && app_cycle_windows::children()?.is_empty()
                {
                    return Ok::<_, anyhow::Error>(());
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await??;
        for phase in ["scan", "cpu", "disk"] {
            let permit = tokio::time::timeout(
                Duration::from_secs(2),
                app.resource_permit(&source_task.id, phase, &CancellationToken::new()),
            )
            .await??;
            drop(permit);
        }
        ensure!(
            app.network.available_permits() == 3
                && app.capture.available_permits() == 1
                && app.processor.available_permits() == 2,
            "A scheduling permit survived task cleanup"
        );
        ensure!(
            app.queued_slots.lock().unwrap().values().all(Vec::is_empty),
            "A resource wait queue survived task cleanup"
        );
        ensure!(
            app.db.tasks_including_retained()?.len() == 1 && app.db.media()?.len() == 1,
            "Catalog grew across resource cycles"
        );
        ensure!(
            std::fs::read_dir(app.download_directory())?.count() == 1,
            "Compression outputs survived deletion"
        );
        ensure!(
            std::fs::read_dir(app.config.data_dir.join("tasks"))?
                .next()
                .is_none(),
            "A task workspace survived deletion"
        );
        ensure!(
            std::fs::read_dir(app.config.data_dir.join("task-logs"))?.count() <= 1,
            "A task log survived deletion"
        );
        ensure!(
            hex::encode(Sha256::digest(std::fs::read(&source)?)) == source_hash,
            "A resource cycle modified its source"
        );
        app.task_log_writer.flush()?;
        if round + 1 == warmup_rounds {
            ensure!(
                history_payload.is_object(),
                "Warmup produced no real task history payload"
            );
            for _ in 0..2048 {
                app.emit("task.upsert", history_payload.clone());
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        let mut sample = app_cycle_windows::snapshot()?;
        sample["round"] = json!(round);
        sample["kind"] = json!(kind);
        sample["elapsed_ms"] = json!(started.elapsed().as_secs_f64() * 1000.0);
        sample["cancelled_stage"] = json!(cancellation_stage);
        sample["output_bytes"] = json!(output_bytes);
        sample["remaining_children"] = json!(app_cycle_windows::children()?);
        sample["runners"] = json!(app.runners.lock().unwrap().len());
        sample["task_records"] = json!(1);
        sample["media_records"] = json!(1);
        let (history_events, history_bytes) = app.event_history.lock().unwrap().retained();
        sample["history_events"] = json!(history_events);
        sample["history_serialized_bytes"] = json!(history_bytes);
        if round < warmup_rounds {
            warmup.push(sample);
        } else {
            samples.push(sample);
        }
        if round + 1 == warmup_rounds {
            baseline = app_cycle_windows::snapshot()?;
        }
    }
    let final_sample = app_cycle_windows::snapshot()?;
    app.shutdown().await?;
    drop(app);
    tokio::time::sleep(Duration::from_millis(200)).await;
    let after_shutdown = app_cycle_windows::snapshot()?;
    let report = json!({"platform":std::env::consts::OS,"architecture":std::env::consts::ARCH,"profile":if cfg!(debug_assertions){"debug"}else{"release"},"scope":"real App CPU compression of one fixed 320x180/8fps/2sec H.264 source; no network/GPU/desktop; completed outputs, cancelled workspaces and task logs deleted each round; bounded event history is retained and measured","history_preheat":"after six real warmup cycles, emit the actual completed task payload 2048 times once to warm the bounded history; no history is cleared during measurement","warmup_rounds":warmup_rounds,"measured_rounds":30,"analysis_threads":2,"source_sha256":source_hash,"source_bytes":source_bytes,"completed_total":completed_count,"cancelled_total":cancelled_count,"retried_total":retry_count,"baseline":baseline,"final":final_sample,"after_shutdown_and_drop":after_shutdown,"warmup":warmup,"samples":samples,"all_owned_children_exited":true,"all_resource_permits_reusable":true,"catalog_records_after_every_round":{"tasks":1,"media":1}});
    let output = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../output/performance-20261020/app-resource-cycles.json");
    std::fs::create_dir_all(output.parent().unwrap())?;
    std::fs::write(&output, serde_json::to_vec_pretty(&report)?)?;
    println!("App resource cycles: {}", output.display());
    Ok(())
}

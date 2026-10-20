use crate::{App, model::Task, process};
use anyhow::{Result, bail};
use std::{path::Path, sync::Arc};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio_util::sync::CancellationToken;

pub(crate) struct Resources {
    scan: Arc<Semaphore>,
    disk: Arc<Semaphore>,
    cpu: Arc<Semaphore>,
    gpu: Arc<Semaphore>,
    threads: Arc<Semaphore>,
    thread_count: u32,
    dual_gpu: tokio::sync::OnceCell<bool>,
}

impl Default for Resources {
    fn default() -> Self {
        let thread_count = std::thread::available_parallelism()
            .map_or(1, usize::from)
            .saturating_sub(1)
            .max(1) as u32;
        Self {
            scan: Arc::new(Semaphore::new(1)),
            disk: Arc::new(Semaphore::new(1)),
            cpu: Arc::new(Semaphore::new(1)),
            gpu: Arc::new(Semaphore::new(2)),
            threads: Arc::new(Semaphore::new(thread_count as usize)),
            thread_count,
            dual_gpu: Default::default(),
        }
    }
}

pub(crate) struct ResourcePermit {
    _stage: OwnedSemaphorePermit,
    _threads: Option<OwnedSemaphorePermit>,
    pub(crate) analysis_threads: usize,
}

fn host_can_overlap(cores: usize, free: Option<u64>) -> bool {
    cores >= 8 && free.is_some_and(|bytes| bytes >= 8 * 1024 * 1024 * 1024)
}

pub(crate) fn analysis_threads(cores: usize, configured: Option<u64>) -> usize {
    configured
        .filter(|value| (1..=256).contains(value))
        .map_or(cores.saturating_sub(1).clamp(1, 8), |value| value as usize)
}

#[cfg(windows)]
fn free_memory() -> Option<u64> {
    #[repr(C)]
    struct MemoryStatus {
        length: u32,
        load: u32,
        total_physical: u64,
        available_physical: u64,
        total_page: u64,
        available_page: u64,
        total_virtual: u64,
        available_virtual: u64,
        extended: u64,
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GlobalMemoryStatusEx(status: *mut MemoryStatus) -> i32;
    }
    let mut status = MemoryStatus {
        length: std::mem::size_of::<MemoryStatus>() as u32,
        load: 0,
        total_physical: 0,
        available_physical: 0,
        total_page: 0,
        available_page: 0,
        total_virtual: 0,
        available_virtual: 0,
        extended: 0,
    };
    (unsafe { GlobalMemoryStatusEx(&mut status) } != 0).then_some(status.available_physical)
}

#[cfg(not(windows))]
fn free_memory() -> Option<u64> {
    // shortcut: resource discovery is Windows-only, enable other platforms after hardware validation.
    None
}

impl App {
    pub(crate) async fn processing_permit(
        &self,
        id: &str,
        cancel: &CancellationToken,
    ) -> Result<OwnedSemaphorePermit> {
        let cores = std::thread::available_parallelism().map_or(1, usize::from);
        let manual = self.db.settings()?["analysisThreads"].as_u64().unwrap_or(0);
        let overlap =
            host_can_overlap(cores, free_memory()) && (manual == 0 || manual < cores as u64);
        self.task_permit_many(
            id,
            "processor",
            &self.processor,
            if overlap { 1 } else { 2 },
            cancel,
        )
        .await
    }

    pub(crate) async fn resource_permit(
        &self,
        id: &str,
        phase: &'static str,
        cancel: &CancellationToken,
    ) -> Result<ResourcePermit> {
        let resource = match phase {
            "scan" => &self.resources.scan,
            "disk" => &self.resources.disk,
            "cpu" => &self.resources.cpu,
            _ => bail!("Unknown processing resource"),
        };
        let total = self.resources.thread_count;
        let requested = match phase {
            "cpu" => total,
            "scan" => analysis_threads(
                total as usize + 1,
                self.db.settings()?["analysisThreads"].as_u64(),
            ) as u32,
            _ => 0,
        };
        let count = requested.min(total);
        let threads = if count > 0 {
            Some(
                self.task_permit_many(id, "threads", &self.resources.threads, count, cancel)
                    .await?,
            )
        } else {
            None
        };
        Ok(ResourcePermit {
            _stage: self.task_permit(id, phase, resource, cancel).await?,
            _threads: threads,
            analysis_threads: requested as usize,
        })
    }

    pub(crate) async fn encoder_probe_permit(
        &self,
        id: &str,
        gpu: bool,
        cancel: &CancellationToken,
    ) -> Result<OwnedSemaphorePermit> {
        // One-frame checks use bounded threads and must not wait for a full scan's thread budget.
        if gpu {
            self.task_permit_many(id, "gpu", &self.resources.gpu, 2, cancel)
                .await
        } else {
            self.task_permit(id, "cpu", &self.resources.cpu, cancel)
                .await
        }
    }

    pub(crate) async fn gpu_permit(
        &self,
        id: &str,
        dual_eligible: bool,
        task: &Task,
        cancel: &CancellationToken,
    ) -> Result<ResourcePermit> {
        let cores = std::thread::available_parallelism().map_or(1, usize::from);
        let allowed = cfg!(windows)
            && dual_eligible
            && host_can_overlap(cores, free_memory())
            && self.gpu_memory_available(cancel).await;
        let dual = if allowed {
            let initialize = self.resources.dual_gpu.get_or_try_init(|| async {
                let _exclusive = self
                    .task_permit_many(id, "gpu", &self.resources.gpu, 2, cancel)
                    .await?;
                let ffmpeg = self.task_tool(task, "ffmpeg").await?;
                // Keep both encoder contexts open long enough to exercise session limits.
                let args: Vec<String> = [
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=1280x720:rate=30",
                    "-frames:v",
                    "30",
                    "-an",
                    "-c:v",
                    "hevc_nvenc",
                    "-f",
                    "null",
                    "-",
                ]
                .into_iter()
                .map(str::to_string)
                .collect();
                let check = async {
                    let (left, right) = tokio::join!(
                        process::run(&ffmpeg, &args, None, cancel, None),
                        process::run(&ffmpeg, &args, None, cancel, None)
                    );
                    if cancel.is_cancelled() {
                        bail!("Operation interrupted");
                    }
                    Ok::<_, anyhow::Error>(left.is_ok() && right.is_ok())
                };
                tokio::time::timeout(std::time::Duration::from_secs(15), check)
                    .await
                    .unwrap_or(Ok(false))
            });
            *tokio::select! {
                result = initialize => result?,
                _ = cancel.cancelled() => bail!("Operation interrupted"),
            }
        } else {
            false
        };
        let count = (self.resources.thread_count / 2)
            .clamp(3, 6)
            .min(self.resources.thread_count);
        let threads = self
            .task_permit_many(id, "threads", &self.resources.threads, count, cancel)
            .await?;
        Ok(ResourcePermit {
            _stage: self
                .task_permit_many(
                    id,
                    "gpu",
                    &self.resources.gpu,
                    if dual { 1 } else { 2 },
                    cancel,
                )
                .await?,
            _threads: Some(threads),
            analysis_threads: 0,
        })
    }

    async fn gpu_memory_available(&self, cancel: &CancellationToken) -> bool {
        let args: Vec<String> = [
            "--id=0",
            "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
        ]
        .into_iter()
        .map(str::to_string)
        .collect();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(3),
            process::run(Path::new("nvidia-smi"), &args, None, cancel, None),
        )
        .await;
        matches!(result, Ok(Ok(output)) if output.stdout.trim().parse::<u64>().is_ok_and(|mib| mib >= 4096))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn overlap_requires_known_resources_above_both_limits() {
        assert!(!host_can_overlap(7, Some(u64::MAX)));
        assert!(!host_can_overlap(8, None));
        assert!(!host_can_overlap(8, Some(8 * 1024 * 1024 * 1024 - 1)));
        assert!(host_can_overlap(8, Some(8 * 1024 * 1024 * 1024)));
        for cores in [1, 2, 4, 8, 16, 128] {
            assert!((1..=8).contains(&analysis_threads(cores, None)));
            assert_eq!(
                analysis_threads(cores, Some(0)),
                analysis_threads(cores, None)
            );
            assert_eq!(analysis_threads(cores, Some(64)), 64);
            assert_eq!(analysis_threads(cores, Some(256)), 256);
            assert_eq!(
                analysis_threads(cores, Some(257)),
                analysis_threads(cores, None)
            );
        }
    }

    #[tokio::test]
    async fn resources_return_after_cancellation_and_cpu_tasks_remain_serial() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        for _ in 0..30 {
            let cancel = CancellationToken::new();
            let first = app.resource_permit(&task.id, "cpu", &cancel).await?;
            let waiting = app.resource_permit(&task.id, "cpu", &cancel);
            tokio::pin!(waiting);
            assert!(
                tokio::time::timeout(std::time::Duration::from_millis(1), &mut waiting)
                    .await
                    .is_err()
            );
            cancel.cancel();
            assert!(waiting.await.is_err());
            drop(first);
            assert_eq!(app.resources.cpu.available_permits(), 1);
            assert_eq!(
                app.resources.threads.available_permits(),
                app.resources.thread_count as usize
            );
            let gpu = app
                .gpu_permit(&task.id, false, &task, &CancellationToken::new())
                .await?;
            assert_eq!(app.resources.gpu.available_permits(), 0);
            drop(gpu);
            assert_eq!(app.resources.gpu.available_permits(), 2);
        }
        assert!(app.queued_slots.lock().unwrap().values().all(Vec::is_empty));
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn completed_runners_are_reaped_across_thirty_retries() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        for _ in 0..30 {
            task.stage("queued", "queued", None);
            app.save_task(&task)?;
            app.schedule(&task.id)?;
            tokio::time::timeout(std::time::Duration::from_secs(2), async {
                while !app.runners.lock().unwrap().is_empty() {
                    tokio::task::yield_now().await;
                }
            })
            .await?;
            assert_eq!(app.db.task(&task.id)?.status, "failed");
            assert_eq!(app.network.available_permits(), 3);
            assert_eq!(app.processor.available_permits(), 2);
        }
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn dropping_one_waiter_does_not_remove_another_for_the_same_task() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let semaphore = Arc::new(Semaphore::new(0));
        let start = || {
            let (app, id, semaphore) = (app.clone(), task.id.clone(), semaphore.clone());
            tokio::spawn(async move {
                app.task_permit(&id, "same-task", &semaphore, &CancellationToken::new())
                    .await
            })
        };
        let first = start();
        let second = start();
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while app
                .queued_slots
                .lock()
                .unwrap()
                .get("same-task")
                .map_or(0, Vec::len)
                != 2
            {
                tokio::task::yield_now().await;
            }
        })
        .await?;
        first.abort();
        assert!(first.await.unwrap_err().is_cancelled());
        assert_eq!(app.queued_slots.lock().unwrap()["same-task"].len(), 1);
        semaphore.add_permits(1);
        let permit = tokio::time::timeout(std::time::Duration::from_secs(2), second).await???;
        assert!(app.queued_slots.lock().unwrap()["same-task"].is_empty());
        drop(permit);
        assert_eq!(semaphore.available_permits(), 1);
        app.shutdown().await?;
        Ok(())
    }
}

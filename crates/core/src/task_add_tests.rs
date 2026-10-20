use super::*;
use std::time::Duration;

#[tokio::test]
async fn collection_expansion_does_not_block_queue_controls() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(AppConfig {
        data_dir: root.path().join("data"),
        download_dir: root.path().join("downloads"),
        tool_dir: None,
    })
    .await?;
    let task = tests::paused_task(Default::default());
    app.db.insert_task(&task)?;
    let network = app.network.acquire_many(3).await?;
    let adding = app.command(
        "tasks.add",
        json!({"urls":["https://example.test/playlist"],"options":{"collectionMode":"all"}}),
    );
    tokio::pin!(adding);
    assert!(futures_util::poll!(&mut adding).is_pending());
    tokio::time::timeout(
        Duration::from_secs(1),
        app.command("tasks.cancel", json!({"id":task.id})),
    )
    .await??;
    assert_eq!(app.db.task(&task.id)?.status, "cancelled");
    app.shutdown_token.cancel();
    assert!(adding.await.is_err());
    drop(network);
    app.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn batch_add_deduplicates_aliases_and_preserves_active_tasks_with_linear_reads() -> Result<()>
{
    let root = tempfile::tempdir()?;
    let app = App::open(AppConfig {
        data_dir: root.path().join("data"),
        download_dir: root.path().join("downloads"),
        tool_dir: None,
    })
    .await?;
    let mut ids = Vec::new();
    let mut urls = Vec::new();
    for index in 0..80 {
        let video_id = format!("video{index:06}");
        let mut task = tests::paused_task(Default::default());
        task.url = format!("https://www.youtube.com/watch?v={video_id}&list=one");
        task.options.quality = "best".into();
        app.db.insert_task(&task)?;
        ids.push(task.id.clone());
        urls.push(task.url.clone());
        urls.push(format!("https://youtu.be/{video_id}"));
        if index == 0 {
            let mut completed = task.clone();
            completed.id = uuid::Uuid::new_v4().to_string();
            completed.stage("completed", "completed", Some(100.0));
            app.db.insert_task(&completed)?;
            let mut conversion = task;
            conversion.id = uuid::Uuid::new_v4().to_string();
            conversion.conversion = true;
            app.db.insert_task(&conversion)?;
        }
    }
    let reads = app.db.task_read_count();
    let result = app
        .command(
            "tasks.add",
            json!({"urls":urls,"options":{"quality":"worst"},"report":true}),
        )
        .await?;
    let reads = app.db.task_read_count() - reads;
    assert_eq!(result["ids"], json!(ids));
    assert_eq!(result["results"].as_array().unwrap().len(), 80);
    assert!(
        result["results"]
            .as_array()
            .unwrap()
            .iter()
            .all(|result| { result["action"] == "existing" && result["status"] == "paused" })
    );
    assert!(reads <= 81, "batch performed {reads} individual task reads");
    assert_eq!(app.db.tasks()?.len(), 82);
    assert_eq!(app.db.task(&ids[0])?.options.quality, "best");
    app.shutdown().await?;
    Ok(())
}

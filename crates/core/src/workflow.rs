use crate::model::Task;

pub(crate) fn plan(task: &mut Task) {
    if !task.steps.is_empty() || task.status == "completed" {
        return;
    }
    task.steps = if task.conversion {
        vec!["verify_sources", "process", "verify_output", "publish"]
    } else if task.kind == "assets" {
        if task.options.write_thumbnail || !task.options.languages().is_empty() {
            vec![
                "metadata",
                "download",
                "verify_sources",
                "process",
                "publish",
            ]
        } else {
            vec!["metadata", "download", "verify_sources", "publish"]
        }
    } else {
        vec![
            "metadata",
            "download",
            "verify_sources",
            "process",
            "verify_output",
            "publish",
        ]
    }
    .into_iter()
    .map(str::to_string)
    .collect();
    task.step_index = position(&task.stage, &task.steps);
}

pub(crate) fn position(stage: &str, steps: &[String]) -> Option<usize> {
    if steps.is_empty() {
        return None;
    }
    if stage == "completed" {
        return Some(steps.len());
    }
    let step = if stage == "probing" || matches!(stage, "probe" | "metadata") {
        "metadata"
    } else if stage.starts_with("downloading_") || matches!(stage, "download" | "downloading") {
        "download"
    } else if stage == "verifying_sources" {
        "verify_sources"
    } else if matches!(stage, "validate" | "verifying_output") {
        "verify_output"
    } else if matches!(
        stage,
        "publishing" | "publish" | "publication" | "validating_publication"
    ) {
        "publish"
    } else if stage.starts_with("converting_subtitle_")
        || stage.starts_with("splitting_chapter_")
        || matches!(
            stage,
            "subtitles"
                | "thumbnail"
                | "cover"
                | "prepend_cover"
                | "muxing"
                | "encoding"
                | "merge"
                | "transcode"
        )
    {
        "process"
    } else {
        return None;
    };
    steps
        .iter()
        .position(|value| value == step)
        .map(|index| index + 1)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn workflow_tracks_major_phases_and_preserves_pause_and_queue() {
        let mut task = crate::tests::paused_task(Default::default());
        task.stage = "probing".into();
        plan(&mut task);
        assert_eq!(task.step_index, Some(1));
        task.stage("downloading", "downloading_audio_2", Some(50.0));
        assert_eq!(task.step_index, Some(2));
        task.stage("paused", "paused", None);
        assert_eq!(task.step_index, Some(2));
        task.stage("processing", "verifying_output", None);
        assert_eq!(task.step_index, Some(5));
        task.stage("completed", "completed", Some(100.0));
        assert_eq!(task.step_index, Some(6));
        assert_eq!(position("queued", &task.steps), None);
        let mut conversion = crate::tests::paused_task(Default::default());
        conversion.conversion = true;
        plan(&mut conversion);
        assert_eq!(conversion.steps.len(), 4);
        assert!(!conversion.steps.iter().any(|step| step == "download"));
    }
}

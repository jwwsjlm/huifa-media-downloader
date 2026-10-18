fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "command",
            "capabilities",
            "open_download_directory",
            "choose_directory",
            "result_action",
            "upload_asset",
            "notify",
            "check_update",
            "install_update",
            "task_copy_value",
            "release_status",
            "open_release",
            "open_project",
            "login_navigation",
        ]),
    ))
    .expect("Could not build desktop command permissions")
}

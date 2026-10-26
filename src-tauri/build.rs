fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .windows_attributes(tauri_build::WindowsAttributes::new_without_app_manifest())
            .app_manifest(tauri_build::AppManifest::new().commands(&[
                "command",
                "capabilities",
                "open_download_directory",
                "choose_directory",
                "result_action",
                "upload_asset",
                "notify",
                "check_update",
                "download_update",
                "update_status",
                "install_update",
                "task_copy_value",
                "release_status",
                "open_release",
                "open_project",
                "login_navigation",
            ])),
    )
    .expect("Could not build desktop command permissions");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        // Embed Common Controls v6 in applications and native-dialog tests through the linker.
        println!("cargo:rustc-link-arg=/MANIFEST:EMBED");
        println!(
            "cargo:rustc-link-arg=/MANIFESTDEPENDENCY:type='win32' name='Microsoft.Windows.Common-Controls' version='6.0.0.0' processorArchitecture='*' publicKeyToken='6595b64144ccf1df' language='*'"
        );
    }
}

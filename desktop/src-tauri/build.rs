fn main() {
 println!("cargo:rerun-if-env-changed=GITHUB_SHA");
 println!("cargo:rustc-env=MARA_BUILD={}", std::env::var("GITHUB_SHA").unwrap_or_else(|_| "development".into()));
 tauri_build::build()
}

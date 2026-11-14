#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{io::{BufRead, BufReader}, path::PathBuf, process::{Child, Command, Stdio}, sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}, time::Duration};
use tauri::{Manager, WindowEvent};
#[derive(Default)]
struct Engine { url: Option<String>, child: Option<Child> }
fn remembered_workspace() -> Option<String> {
 let home=std::env::var_os("HOME")?;
 let file=PathBuf::from(home).join("Library/Application Support/MARA/last-workspace.json");
 let data=std::fs::read_to_string(file).ok()?;
 let workspace=serde_json::from_str::<serde_json::Value>(&data).ok()?["workspace"].as_str()?.to_owned();
 PathBuf::from(&workspace).is_dir().then_some(workspace)
}
fn main() {
 let engine=Arc::new(Mutex::new(Engine::default()));
 let closing=Arc::new(AtomicBool::new(false));
 let boot=engine.clone(); let close_engine=engine.clone();
 tauri::Builder::default().setup(move |app| {
  let handle=app.handle().clone();
  std::thread::spawn(move || {
   let result=(|| -> Result<String,Box<dyn std::error::Error>> {
    let client=reqwest::blocking::Client::builder().timeout(Duration::from_secs(2)).build()?;
    // Reuse this workspace’s running service, including an active run.
    let existing=client.get("http://127.0.0.1:8765/api/health").send().and_then(|r| r.json::<serde_json::Value>());
    // The browser UI and native app share a durable workspace pointer. This
    // prevents a reconnect from opening a fresh, empty Application Support folder.
    let mut inherited_workspace=std::env::var("MARA_WORKSPACE").ok().or_else(remembered_workspace);
    if let Ok(h)=existing { if h["app"]=="MARA" && std::env::var("MARA_WORKSPACE").ok().map(|w| h["workspace"]==w).unwrap_or(true) {
     if h["build"]==env!("MARA_BUILD") {return Ok("http://127.0.0.1:8765".into());}
     let state=client.get("http://127.0.0.1:8765/api/state").send()?.json::<serde_json::Value>()?;
     if state["active"]==true || state["running"]==true || state["engine"]["active"]==true {
      if let Some(w)=handle.get_webview_window("main") {let _=w.set_title("MARA · Reopen after this run to activate the update");}
      return Ok("http://127.0.0.1:8765".into());
     }
     inherited_workspace=h["workspace"].as_str().map(str::to_owned);
    } }
    let exe=std::env::current_exe()?.parent().ok_or("Missing app directory")?.join(if cfg!(windows){"mara-engine.exe"}else{"mara-engine"});
    let mut cmd=Command::new(exe);cmd.args(["desktop-service"]).stdout(Stdio::piped()).stderr(Stdio::inherit());
    if let Some(workspace)=inherited_workspace {cmd.env("MARA_WORKSPACE",workspace);}
    #[cfg(windows)] {use std::os::windows::process::CommandExt;cmd.creation_flags(0x08000000);}
    let mut child=cmd.spawn()?;let out=child.stdout.take().ok_or("Missing engine output")?;
    boot.lock().unwrap().child=Some(child);
    for line in BufReader::new(out).lines() {let line=line?;if let Some(rest)=line.strip_prefix("MARA: ").or_else(||line.strip_prefix("MARA is already running: ")) {if let Some(url)=rest.split_whitespace().next(){if url.starts_with("http://127.0.0.1:"){return Ok(url.to_owned());}}}}
    Err("The audio engine stopped before it was ready. Check the engine log in your MARA data folder.".into())
   })();
   if let Some(window)=handle.get_webview_window("main") {match result {Ok(url)=>{boot.lock().unwrap().url=Some(url.clone());let _=window.navigate(format!("{url}/workflow").parse().unwrap());},Err(err)=>{let text=serde_json::to_string(&format!("Could not start MARA: {err}")).unwrap();let _=window.eval(&format!("document.getElementById('startup').textContent={text}"));}}}
  });Ok(())
 }).on_window_event(move |window,event| {
  if let WindowEvent::CloseRequested{api,..}=event {
   api.prevent_close();if closing.swap(true,Ordering::SeqCst){return;}
   let handle=window.app_handle().clone();let engine=close_engine.clone();let reset=closing.clone();
   std::thread::spawn(move || {
    let client=reqwest::blocking::Client::builder().timeout(Duration::from_secs(10)).build().unwrap();
    let url=engine.lock().unwrap().url.clone();
    if let Some(url)=url {
     let status=client.get(format!("{url}/api/state")).send().and_then(|r|r.json::<serde_json::Value>());
     if status.as_ref().map(|s|s["active"]==true || s["running"]==true || s["engine"]["active"]==true).unwrap_or(false) {
      if client.post(format!("{url}/api/stop")).json(&serde_json::json!({})).send().map(|r|r.status().is_success()).unwrap_or(false) {
       // A safe pause can outlast a model request. Keep the window and engine alive until acknowledged.
       loop {std::thread::sleep(Duration::from_millis(750));let status=client.get(format!("{url}/api/state")).send().and_then(|r|r.json::<serde_json::Value>());match status {Ok(s) if s["active"]==false && s["running"]==false && s["engine"]["active"]!=true=>break,Ok(_)=>continue,Err(_)=>{reset.store(false,Ordering::SeqCst);return;}}}
      } else {reset.store(false,Ordering::SeqCst);return;}
     } else if status.is_err(){reset.store(false,Ordering::SeqCst);return;}
    }
    // Engine services may be shared with another window. They stay available for reconnect.
    handle.exit(0);
   });
  }
 }).run(tauri::generate_context!()).expect("MARA desktop could not start");
}

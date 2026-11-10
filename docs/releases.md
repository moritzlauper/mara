# Desktop releases

[Download the latest development build](https://github.com/moritzlauper/mara/releases/tag/latest).

The **Desktop packages** GitHub Actions workflow builds installers for macOS Apple Silicon, macOS Intel, and Windows x64. Each platform runs the Python test suite, bundles the engine, checks that the bundled executable starts, and builds the Tauri application. Publication requires all three platform jobs and all four installers to succeed.

## Automatic development release

Every push to `main` triggers the workflow. Successful runs refresh the existing `latest` prerelease, its source tag, installer downloads, SHA-256 checksums, and build details. The release page and download filenames remain stable. An older build will not publish if `main` has advanced to a newer commit.

The desktop installer version uses the configured major/minor version and increments its patch component by the workflow run number. This distinguishes successive development builds without modifying the source manifests. The Python package retains its source version.

To rebuild manually, open **Actions → Desktop packages → Run workflow**, select `main`, and start the run. Dispatches on other branches build artifacts without publishing a release.

## Versioned releases

Push a tag in the form `desktop-v0.1.0` to build and publish a separate versioned release. The numeric tag determines the installer version. Published versioned releases are not overwritten; use a new version tag for a subsequent release.

## Permissions and distribution

The build jobs have read-only repository access. Only the publication job receives `contents: write` through the workflow's `GITHUB_TOKEN`; no personal access token is required. Keep the `latest` release mutable so that the rolling tag and assets can be updated.

Current installers are unsigned, and the macOS applications are not notarized. The workflow updates GitHub downloads; it does not install updates into an already-running MARA application. Ableton Live and Max for Live are required separately.

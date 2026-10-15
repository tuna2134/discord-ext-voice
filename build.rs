//! Build script: link the libav* libraries this crate calls directly.
//!
//! `ac-ffmpeg` brings in the whole FFmpeg stack for demuxing and decoding, but its
//! frame-based resampler relies on `swr_convert_frame`, whose per-frame parameter check
//! rejects the unspecified channel layouts that modern FFmpeg decoders produce. The audio
//! path therefore drives libswresample's buffer-based `swr_convert` API itself, which needs
//! these two libraries to be linked explicitly.

fn main() {
    println!("cargo:rerun-if-changed=build.rs");

    for library in ["swresample", "avutil"] {
        println!("cargo:rustc-link-lib={library}");
    }

    // Prefer the pkg-config search path when it is available so that non-standard FFmpeg
    // installations are found.
    if let Ok(output) = std::process::Command::new("pkg-config")
        .args(["--libs", "libswresample", "libavutil"])
        .output()
        && output.status.success()
    {
        let flags = String::from_utf8_lossy(&output.stdout);
        for flag in flags.split_whitespace() {
            if let Some(path) = flag.strip_prefix("-L") {
                println!("cargo:rustc-link-search=native={path}");
            }
        }
    }
}

//! Audio resampling to Discord's 48kHz stereo signed 16 bit format.
//!
//! This talks to libswresample directly instead of going through `ac-ffmpeg`'s
//! `AudioResampler`. The reason is concrete: `AudioResampler` is a thin wrapper around
//! `swr_convert_frame`, which refuses to run when the input frame's channel layout differs
//! from the one the context was configured with — and FFmpeg decoders commonly emit an
//! *unspecified* layout for plain mono/stereo streams. `swr_convert` (the buffer based API)
//! has no such per-frame check, so it happily converts whatever the decoder produces.
//!
//! Only the small, stable part of the libswresample/libavutil C API used here is declared.

use std::ffi::{CStr, c_char, c_int, c_void};
use std::ptr;

/// `AVChannelOrder::AV_CHANNEL_ORDER_NATIVE`.
const AV_CHANNEL_ORDER_NATIVE: c_int = 1;

/// `AVSampleFormat` values, in declaration order.
pub const AV_SAMPLE_FMT_U8: c_int = 0;
pub const AV_SAMPLE_FMT_S16: c_int = 1;
pub const AV_SAMPLE_FMT_S32: c_int = 2;
pub const AV_SAMPLE_FMT_FLT: c_int = 3;
pub const AV_SAMPLE_FMT_DBL: c_int = 4;
pub const AV_SAMPLE_FMT_U8P: c_int = 5;
pub const AV_SAMPLE_FMT_S16P: c_int = 6;
pub const AV_SAMPLE_FMT_S32P: c_int = 7;
pub const AV_SAMPLE_FMT_FLTP: c_int = 8;
pub const AV_SAMPLE_FMT_DBLP: c_int = 9;
pub const AV_SAMPLE_FMT_S64: c_int = 10;
pub const AV_SAMPLE_FMT_S64P: c_int = 11;

/// `av_channel_layout` from `<libavutil/channel_layout.h>`.
///
/// `u` is a union of `uint64_t mask` and `AVChannelCustom *map`; both are eight bytes with
/// eight byte alignment, so a `u64` field has the correct size and layout.
#[repr(C)]
pub struct AVChannelLayout {
    order: c_int,
    nb_channels: c_int,
    u: u64,
    opaque: *mut c_void,
}

impl AVChannelLayout {
    /// A default (native) layout for the given channel count, e.g. 1 -> mono, 2 -> stereo.
    pub fn default_for(channels: u32) -> Self {
        let mut layout = Self {
            order: AV_CHANNEL_ORDER_NATIVE,
            nb_channels: 0,
            u: 0,
            opaque: ptr::null_mut(),
        };
        unsafe { av_channel_layout_default(&mut layout, channels as c_int) };
        layout
    }

    pub fn channels(&self) -> u32 {
        self.nb_channels as u32
    }
}

impl Drop for AVChannelLayout {
    fn drop(&mut self) {
        unsafe { av_channel_layout_uninit(self) }
    }
}

unsafe extern "C" {
    fn av_channel_layout_default(channel_layout: *mut AVChannelLayout, nb_channels: c_int);
    fn av_channel_layout_uninit(channel_layout: *mut AVChannelLayout);
    fn av_strerror(errnum: c_int, errbuf: *mut c_char, errbuf_size: usize) -> c_int;

    fn swr_alloc_set_opts2(
        ps: *mut *mut c_void,
        out_ch_layout: *const AVChannelLayout,
        out_sample_fmt: c_int,
        out_sample_rate: c_int,
        in_ch_layout: *const AVChannelLayout,
        in_sample_fmt: c_int,
        in_sample_rate: c_int,
        log_offset: c_int,
        log_ctx: *mut c_void,
    ) -> c_int;
    fn swr_init(s: *mut c_void) -> c_int;
    fn swr_free(s: *mut *mut c_void);
    fn swr_convert(
        s: *mut c_void,
        out: *mut *mut u8,
        out_count: c_int,
        in_: *const *const u8,
        in_count: c_int,
    ) -> c_int;
    fn swr_get_out_samples(s: *mut c_void, in_samples: c_int) -> c_int;
}

fn error_text(code: c_int) -> String {
    let mut buffer = [0 as c_char; 256];
    unsafe {
        if av_strerror(code, buffer.as_mut_ptr(), buffer.len()) == 0 {
            return CStr::from_ptr(buffer.as_ptr())
                .to_string_lossy()
                .into_owned();
        }
    }
    format!("FFmpeg error {code}")
}

/// Map a sample format *name* (as reported by `ac_ffmpeg`) to its `AVSampleFormat` value.
pub fn sample_format_code(name: &str) -> Option<c_int> {
    Some(match name {
        "u8" => AV_SAMPLE_FMT_U8,
        "s16" => AV_SAMPLE_FMT_S16,
        "s32" => AV_SAMPLE_FMT_S32,
        "flt" => AV_SAMPLE_FMT_FLT,
        "dbl" => AV_SAMPLE_FMT_DBL,
        "u8p" => AV_SAMPLE_FMT_U8P,
        "s16p" => AV_SAMPLE_FMT_S16P,
        "s32p" => AV_SAMPLE_FMT_S32P,
        "fltp" => AV_SAMPLE_FMT_FLTP,
        "dblp" => AV_SAMPLE_FMT_DBLP,
        "s64" => AV_SAMPLE_FMT_S64,
        "s64p" => AV_SAMPLE_FMT_S64P,
        _ => return None,
    })
}

/// Whether an `AVSampleFormat` stores each channel in its own plane.
pub fn is_planar(format: c_int) -> bool {
    format >= AV_SAMPLE_FMT_U8P
}

/// How many bytes one sample of the given format occupies.
pub fn bytes_per_sample(format: c_int) -> usize {
    match format {
        AV_SAMPLE_FMT_U8 | AV_SAMPLE_FMT_U8P => 1,
        AV_SAMPLE_FMT_S16 | AV_SAMPLE_FMT_S16P => 2,
        AV_SAMPLE_FMT_S32 | AV_SAMPLE_FMT_S32P | AV_SAMPLE_FMT_FLT | AV_SAMPLE_FMT_FLTP => 4,
        AV_SAMPLE_FMT_DBL | AV_SAMPLE_FMT_DBLP | AV_SAMPLE_FMT_S64 | AV_SAMPLE_FMT_S64P => 8,
        _ => 2,
    }
}

/// A stereo 48kHz `s16` resampler, driven with raw buffers.
pub struct StereoResampler {
    context: *mut c_void,
    in_format: c_int,
    in_rate: c_int,
    in_channels: u32,
    target_channels: u32,
    target_rate: u32,
}

// SAFETY: `SwrContext` holds no thread affinity; the wrapper only ever touches it while
// `DecodeSource`'s mutex is held.
unsafe impl Send for StereoResampler {}

impl std::fmt::Debug for StereoResampler {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StereoResampler")
            .field("in_format", &self.in_format)
            .field("in_rate", &self.in_rate)
            .field("in_channels", &self.in_channels)
            .field("target_channels", &self.target_channels)
            .field("target_rate", &self.target_rate)
            .finish_non_exhaustive()
    }
}

impl StereoResampler {
    /// Build a resampler from the given decoded-frame parameters to 48kHz stereo `s16`.
    pub fn new(
        in_format_name: &str,
        in_rate: u32,
        in_channels: u32,
        target_rate: u32,
    ) -> Result<Self, String> {
        let in_format = sample_format_code(in_format_name)
            .ok_or_else(|| format!("unsupported audio sample format {in_format_name:?}"))?;
        if in_rate == 0 || in_channels == 0 {
            return Err("the decoder reported an invalid sample rate or channel count".to_owned());
        }

        let out_layout = AVChannelLayout::default_for(2);
        let target_channels = out_layout.channels();
        let in_layout = AVChannelLayout::default_for(in_channels);

        let mut context: *mut c_void = ptr::null_mut();
        let result = unsafe {
            swr_alloc_set_opts2(
                &mut context,
                &out_layout,
                AV_SAMPLE_FMT_S16,
                target_rate as c_int,
                &in_layout,
                in_format,
                in_rate as c_int,
                0,
                ptr::null_mut(),
            )
        };
        if result < 0 || context.is_null() {
            return Err(format!(
                "swresample could not be configured: {}",
                error_text(result)
            ));
        }

        let init = unsafe { swr_init(context) };
        if init < 0 {
            unsafe { swr_free(&mut context) };
            return Err(format!(
                "swresample could not be initialized: {}",
                error_text(init)
            ));
        }

        Ok(Self {
            context,
            in_format,
            in_rate: in_rate as c_int,
            in_channels,
            target_channels,
            target_rate,
        })
    }

    pub fn matches(&self, in_format_name: &str, in_rate: u32, in_channels: u32) -> bool {
        sample_format_code(in_format_name) == Some(self.in_format)
            && in_rate as c_int == self.in_rate
            && in_channels == self.in_channels
    }

    /// Upper bound on the number of output samples per channel for `input_samples` input samples.
    pub fn output_capacity(&self, input_samples: usize) -> usize {
        let capacity = unsafe { swr_get_out_samples(self.context, input_samples as c_int) };
        capacity.max(0) as usize + 64
    }

    /// Convert one decoded frame, appending interleaved stereo `s16` bytes to `output`.
    ///
    /// `planes` must hold one slice per plane for planar formats, or a single slice for
    /// packed formats.
    pub fn convert(
        &mut self,
        planes: &[&[u8]],
        input_samples: usize,
        output: &mut Vec<u8>,
    ) -> Result<(), String> {
        // Guard against a caller handing us the wrong plane layout or a truncated plane.
        let expected_planes = if is_planar(self.in_format) {
            self.in_channels as usize
        } else {
            1
        };
        if planes.len() != expected_planes {
            return Err(format!(
                "expected {expected_planes} audio plane(s) for {} but received {}",
                sample_format_name(self.in_format),
                planes.len()
            ));
        }
        let needed = input_samples * bytes_per_sample(self.in_format);
        if let Some(short) = planes.iter().find(|plane| plane.len() < needed) {
            return Err(format!(
                "audio plane is {} bytes but {needed} were expected for {input_samples} samples                  (format {})",
                short.len(),
                sample_format_name(self.in_format)
            ));
        }

        let capacity = self.output_capacity(input_samples).max(1);
        let mut out_buffer = vec![0i16; capacity * self.target_channels as usize];
        let mut out_ptrs: [*mut u8; 1] = [out_buffer.as_mut_ptr().cast::<u8>()];

        let in_ptrs: Vec<*const u8> = planes.iter().map(|plane| plane.as_ptr()).collect();

        let produced = unsafe {
            swr_convert(
                self.context,
                out_ptrs.as_mut_ptr(),
                capacity as c_int,
                in_ptrs.as_ptr(),
                input_samples as c_int,
            )
        };
        if produced < 0 {
            return Err(format!("swresample failed: {}", error_text(produced)));
        }

        let samples = produced as usize * self.target_channels as usize;
        for sample in &out_buffer[..samples] {
            output.extend_from_slice(&sample.to_le_bytes());
        }
        Ok(())
    }

    /// Drain the samples still buffered inside swresample.
    pub fn flush(&mut self, output: &mut Vec<u8>) -> Result<(), String> {
        loop {
            let capacity = self.output_capacity(0).max(1);
            let mut out_buffer = vec![0i16; capacity * self.target_channels as usize];
            let mut out_ptrs: [*mut u8; 1] = [out_buffer.as_mut_ptr().cast::<u8>()];

            let produced = unsafe {
                swr_convert(
                    self.context,
                    out_ptrs.as_mut_ptr(),
                    capacity as c_int,
                    ptr::null(),
                    0,
                )
            };
            if produced < 0 {
                return Err(format!("swresample flush failed: {}", error_text(produced)));
            }
            if produced == 0 {
                return Ok(());
            }

            let samples = produced as usize * self.target_channels as usize;
            for sample in &out_buffer[..samples] {
                output.extend_from_slice(&sample.to_le_bytes());
            }
        }
    }
}

impl Drop for StereoResampler {
    fn drop(&mut self) {
        if !self.context.is_null() {
            unsafe { swr_free(&mut self.context) };
        }
    }
}

/// Human readable name for an `AVSampleFormat`, used in error messages.
pub fn sample_format_name(code: c_int) -> &'static str {
    match code {
        AV_SAMPLE_FMT_U8 => "u8",
        AV_SAMPLE_FMT_S16 => "s16",
        AV_SAMPLE_FMT_S32 => "s32",
        AV_SAMPLE_FMT_FLT => "flt",
        AV_SAMPLE_FMT_DBL => "dbl",
        AV_SAMPLE_FMT_U8P => "u8p",
        AV_SAMPLE_FMT_S16P => "s16p",
        AV_SAMPLE_FMT_S32P => "s32p",
        AV_SAMPLE_FMT_FLTP => "fltp",
        AV_SAMPLE_FMT_DBLP => "dblp",
        AV_SAMPLE_FMT_S64 => "s64",
        AV_SAMPLE_FMT_S64P => "s64p",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_format_names() {
        assert_eq!(sample_format_code("s16"), Some(AV_SAMPLE_FMT_S16));
        assert_eq!(sample_format_code("fltp"), Some(AV_SAMPLE_FMT_FLTP));
        assert_eq!(sample_format_code("nope"), None);
        assert!(is_planar(AV_SAMPLE_FMT_FLTP));
        assert!(!is_planar(AV_SAMPLE_FMT_FLT));
        assert_eq!(bytes_per_sample(AV_SAMPLE_FMT_S16), 2);
        assert_eq!(bytes_per_sample(AV_SAMPLE_FMT_DBL), 8);
    }

    #[test]
    fn resamples_a_44100_hz_stereo_frame_to_48000_hz() {
        let mut resampler = StereoResampler::new("s16", 44100, 2, 48000).unwrap();
        // 1024 input samples per channel -> ~1115 output samples.
        let input = vec![0u8; 1024 * 4];
        let mut output = Vec::new();
        resampler.convert(&[&input], 1024, &mut output).unwrap();
        resampler.flush(&mut output).unwrap();

        let produced = output.len() / 4;
        let expected = (1024.0f64 * 48000.0 / 44100.0).round() as usize;
        assert!(
            produced.abs_diff(expected) <= 2,
            "expected about {expected} frames, got {produced}"
        );
    }

    #[test]
    fn upmixes_mono_to_stereo() {
        let mut resampler = StereoResampler::new("s16", 48000, 1, 48000).unwrap();

        let samples: Vec<i16> = vec![1000; 960];
        let input: &[u8] =
            unsafe { std::slice::from_raw_parts(samples.as_ptr().cast::<u8>(), samples.len() * 2) };

        let mut output = Vec::new();
        resampler.convert(&[input], 960, &mut output).unwrap();
        assert_eq!(output.len(), 960 * 4);

        // The mono channel is duplicated, so both channels always carry the same value.
        for frame in output.chunks_exact(4) {
            let left = i16::from_le_bytes([frame[0], frame[1]]);
            let right = i16::from_le_bytes([frame[2], frame[3]]);
            assert_eq!(left, right);
            assert!(left > 0, "the signal must survive the upmix, got {left}");
        }
    }

    #[test]
    fn converts_planar_float_input() {
        // stereo planar float, 48kHz -> 48kHz stereo s16
        let mut resampler = StereoResampler::new("fltp", 48000, 2, 48000).unwrap();
        let left: Vec<f32> = vec![0.5; 480];
        let right: Vec<f32> = vec![-0.5; 480];
        let left_bytes: &[u8] =
            unsafe { std::slice::from_raw_parts(left.as_ptr().cast::<u8>(), left.len() * 4) };
        let right_bytes: &[u8] =
            unsafe { std::slice::from_raw_parts(right.as_ptr().cast::<u8>(), right.len() * 4) };
        let mut output = Vec::new();
        resampler
            .convert(&[left_bytes, right_bytes], 480, &mut output)
            .unwrap();
        assert_eq!(output.len(), 480 * 4);
        assert_eq!(i16::from_le_bytes([output[0], output[1]]), 16384);
        assert_eq!(i16::from_le_bytes([output[2], output[3]]), -16384);
    }

    #[test]
    fn rejects_an_unknown_sample_format() {
        assert!(StereoResampler::new("nope", 48000, 2, 48000).is_err());
    }

    #[test]
    fn sample_format_name_round_trips() {
        for name in [
            "u8", "s16", "s32", "flt", "dbl", "u8p", "s16p", "s32p", "fltp", "dblp",
        ] {
            let code = sample_format_code(name).unwrap();
            assert_eq!(sample_format_name(code), name);
        }
    }
}

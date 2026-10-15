#include <libavutil/channel_layout.h>
#include <libavutil/frame.h>
#include <libswresample/swresample.h>

typedef struct AudioResampler {
    struct SwrContext* resample_context;
    struct AVFrame* tmp_frame;
    struct AVFrame* output_frame;

#ifdef FFW_FEATURE_CHANNEL_LAYOUT_V2
    AVChannelLayout target_channel_layout;
#else
    uint64_t target_channel_layout;
#endif

    int target_channels;
    int target_sample_format;
    int target_sample_rate;
    int target_frame_samples;
    int source_sample_rate;
    int tmp_frame_capacity;

    int64_t source_samples;
    int64_t expected_source_pts;
    int64_t output_samples;
    int64_t input_pts_offset;
    int64_t output_pts_offset;

    int offset;
    int flush;
} AudioResampler;

static AVFrame* alloc_frame(
#ifdef FFW_FEATURE_CHANNEL_LAYOUT_V2
    const AVChannelLayout* channel_layout,
#else
    const uint64_t* channel_layout,
#endif
    int sample_fmt,
    int sample_rate,
    int nb_samples)
{
    AVFrame* frame = av_frame_alloc();

    if (!frame) {
        return NULL;
    }

    frame->format = sample_fmt;
    frame->sample_rate = sample_rate;
    frame->nb_samples = nb_samples;

#ifdef FFW_FEATURE_CHANNEL_LAYOUT_V2
    if (av_channel_layout_copy(&frame->ch_layout, channel_layout) != 0) {
        goto err;
    }
#else
    frame->channel_layout = *channel_layout;
    frame->channels = av_get_channel_layout_nb_channels(*channel_layout);
#endif

    if (av_frame_get_buffer(frame, 0) != 0) {
        goto err;
    }

    return frame;

err:
    av_frame_free(&frame);

    return NULL;
}

void ffw_audio_resampler_free(AudioResampler* resampler);

#ifdef FFW_FEATURE_CHANNEL_LAYOUT_V2
AudioResampler* ffw_audio_resampler_new(
    const AVChannelLayout* target_channel_layout,
    int target_sample_format,
    int target_sample_rate,
    int target_frame_samples,
    const AVChannelLayout* source_channel_layout,
    int source_sample_format,
    int source_sample_rate) {
    AudioResampler* res;
    int ret;

    if (!(res = calloc(1, sizeof(AudioResampler)))) {
        return NULL;
    }

    if (av_channel_layout_copy(&res->target_channel_layout, target_channel_layout) != 0) {
        goto err;
    }

    res->target_channels = target_channel_layout->nb_channels;
    res->target_sample_format = target_sample_format;
    res->target_sample_rate = target_sample_rate;
    res->target_frame_samples = target_frame_samples;
    res->source_sample_rate = source_sample_rate;

    ret = swr_alloc_set_opts2(
        &res->resample_context,
        (AVChannelLayout*)target_channel_layout,
        target_sample_format,
        target_sample_rate,
        (AVChannelLayout*)source_channel_layout,
        source_sample_format,
        source_sample_rate,
        0,
        NULL);

    if (ret != 0 || !res->resample_context) {
        goto err;
    }

    if (swr_init(res->resample_context) < 0) {
        goto err;
    }

    return res;

err:
    ffw_audio_resampler_free(res);

    return NULL;
}
#else
AudioResampler* ffw_audio_resampler_new(
    const uint64_t* target_channel_layout,
    int target_sample_format,
    int target_sample_rate,
    int target_frame_samples,
    const uint64_t* source_channel_layout,
    int source_sample_format,
    int source_sample_rate) {
    AudioResampler* res = calloc(1, sizeof(AudioResampler));

    if (!res) {
        return NULL;
    }

    res->target_channel_layout = *target_channel_layout;
    res->target_channels = av_get_channel_layout_nb_channels(*target_channel_layout);
    res->target_sample_format = target_sample_format;
    res->target_sample_rate = target_sample_rate;
    res->target_frame_samples = target_frame_samples;
    res->source_sample_rate = source_sample_rate;

    res->resample_context = swr_alloc_set_opts(
        NULL,
        *target_channel_layout,
        target_sample_format,
        target_sample_rate,
        *source_channel_layout,
        source_sample_format,
        source_sample_rate,
        0,
        NULL);

    if (!res->resample_context) {
        goto err;
    }

    if (swr_init(res->resample_context) < 0) {
        goto err;
    }

    return res;

err:
    ffw_audio_resampler_free(res);

    return NULL;
}
#endif

/// Return a shallow copy of `frame` whose channel layout has been replaced by the default
/// layout for its channel count, or NULL when the frame's layout is already usable.
///
/// Decoders routinely leave the channel layout of plain PCM streams unspecified (for example
/// "2 channels, order unspecified"). `swr_alloc_set_opts2` resolves such a layout to the
/// default one for that channel count, so `swr_convert_frame` sees the frame's unspecified
/// layout as a change of input parameters, tries to reconfigure the context and fails with
/// `AVERROR_INPUT_CHANGED`. Handing it a frame with the resolved layout avoids that.
static AVFrame* frame_with_default_channel_layout(const AVFrame* frame) {
    AVFrame* copy;
    int channels;

#ifdef FFW_FEATURE_CHANNEL_LAYOUT_V2
    channels = frame->ch_layout.nb_channels;

    if (frame->ch_layout.order != AV_CHANNEL_ORDER_UNSPEC
        && av_channel_layout_check(&frame->ch_layout)) {
        return NULL;
    }
#else
    channels = frame->channels;

    if (frame->channel_layout != 0) {
        return NULL;
    }
#endif

    if (channels <= 0) {
        return NULL;
    }

    // The sample data is reference counted, so the copy only shares it.
    copy = av_frame_clone(frame);

    if (!copy) {
        return NULL;
    }

#ifdef FFW_FEATURE_CHANNEL_LAYOUT_V2
    // `av_channel_layout_uninit` clears `nb_channels` as well, hence the saved value.
    av_channel_layout_uninit(&copy->ch_layout);
    av_channel_layout_default(&copy->ch_layout, channels);
#else
    copy->channel_layout = av_get_default_channel_layout(channels);
#endif

    return copy;
}

int ffw_audio_resampler_push_frame(AudioResampler* resampler, const AVFrame* frame) {
    int required_capacity;
    int ret;

    int64_t pts_delta;

    AVFrame* normalised_frame;
    const AVFrame* input_frame;

    // check if the internal frame has been consumed
    if (resampler->tmp_frame && (resampler->offset < resampler->tmp_frame->nb_samples)) {
        return 0;
    }

    if (frame) {
        // set the initial pts expectation
        if (resampler->source_samples == 0) {
            resampler->expected_source_pts = frame->pts;
            resampler->input_pts_offset = frame->pts;
            resampler->output_pts_offset = frame->pts
                * resampler->target_sample_rate
                / resampler->source_sample_rate;
        }

        pts_delta = frame->pts - resampler->expected_source_pts;

        if (pts_delta > 0) {
            // insert a given number of silence samples if there is a gap
            ret = swr_inject_silence(resampler->resample_context, pts_delta);

            if (ret < 0) {
                return ret;
            }
        } else if (pts_delta < 0) {
            // drop a given number of samples
            ret = swr_drop_output(
                resampler->resample_context,
                -pts_delta * resampler->target_sample_rate / resampler->source_sample_rate);

            if (ret < 0) {
                return ret;
            }
        }

        resampler->source_samples += frame->nb_samples + pts_delta;
        resampler->expected_source_pts = resampler->input_pts_offset + resampler->source_samples;

        required_capacity = swr_get_out_samples(
            resampler->resample_context,
            frame->nb_samples);
    } else {
        resampler->flush = 1;

        required_capacity = swr_get_delay(
                resampler->resample_context,
                resampler->target_sample_rate) + 3;
    }

    if (required_capacity < 0) {
        return required_capacity;
    }

    if (!resampler->tmp_frame
        || !av_frame_is_writable(resampler->tmp_frame)
        || (required_capacity > resampler->tmp_frame_capacity)) {
        av_frame_free(&resampler->tmp_frame);

        resampler->tmp_frame = alloc_frame(
            &resampler->target_channel_layout,
            resampler->target_sample_format,
            resampler->target_sample_rate,
            required_capacity);

        if (!resampler->tmp_frame) {
            return -1;
        }

        resampler->tmp_frame_capacity = required_capacity;
    }

    resampler->tmp_frame->nb_samples = 0;
    resampler->offset = 0;

    normalised_frame = frame ? frame_with_default_channel_layout(frame) : NULL;
    input_frame = normalised_frame ? normalised_frame : frame;

    ret = swr_convert_frame(resampler->resample_context, resampler->tmp_frame, input_frame);

    if (normalised_frame) {
        av_frame_free(&normalised_frame);
    }

    if (ret < 0) {
        return ret;
    }

    resampler->tmp_frame->pts = resampler->output_samples + resampler->output_pts_offset;
    resampler->output_samples += resampler->tmp_frame->nb_samples;

    return 1;
}

int ffw_audio_resampler_take_frame(AudioResampler* resampler, AVFrame** frame) {
    int available_samples;
    int required_samples;
    int copy_samples;

    // sanity check
    if (!resampler->tmp_frame) {
        return 0;
    }

    // for non-fixed target frame size, we can just clone the tmp frame
    if (!resampler->target_frame_samples) {
        // reset the flush flag
        resampler->flush = 0;

        if (resampler->tmp_frame->nb_samples > 0) {
            *frame = av_frame_clone(resampler->tmp_frame);
            resampler->tmp_frame->nb_samples = 0;
            return 1;
        } else {
            return 0;
        }
    }

    // check if the internal frame is initialized and writable and create a new
    // one if necessary
    if (!resampler->output_frame || !av_frame_is_writable(resampler->output_frame)) {
        av_frame_free(&resampler->output_frame);

        resampler->output_frame = alloc_frame(
            &resampler->target_channel_layout,
            resampler->target_sample_format,
            resampler->target_sample_rate,
            resampler->target_frame_samples);

        if (!resampler->output_frame) {
            return -1;
        }

        resampler->output_frame->nb_samples = 0;
    }

    required_samples = resampler->target_frame_samples - resampler->output_frame->nb_samples;
    available_samples = resampler->tmp_frame->nb_samples - resampler->offset;

    // check how much we can copy
    if (available_samples > required_samples) {
        copy_samples = required_samples;
    } else {
        copy_samples = available_samples;
    }

    // copy the samples
    if (copy_samples > 0) {
        av_samples_copy(
            resampler->output_frame->extended_data,
            resampler->tmp_frame->extended_data,
            resampler->output_frame->nb_samples,
            resampler->offset,
            copy_samples,
            resampler->target_channels,
            resampler->target_sample_format);

        // set output frame pts
        if (!resampler->output_frame->nb_samples) {
            resampler->output_frame->pts = resampler->tmp_frame->pts + resampler->offset;
        }

        resampler->offset += copy_samples;
        resampler->output_frame->nb_samples += copy_samples;
    }

    if (!resampler->flush) {
        if (resampler->output_frame->nb_samples < resampler->target_frame_samples) {
            return 0;
        }
    }

    *frame = av_frame_clone(resampler->output_frame);

    // reuse the output frame
    resampler->output_frame->nb_samples = 0;

    // reset the flush flag
    if (resampler->offset >= resampler->tmp_frame->nb_samples) {
        resampler->flush = 0;
    }

    return 1;
}

void ffw_audio_resampler_free(AudioResampler* resampler) {
    if (!resampler) {
        return;
    }

    av_frame_free(&resampler->tmp_frame);
    av_frame_free(&resampler->output_frame);
    swr_free(&resampler->resample_context);

#ifdef FFW_FEATURE_CHANNEL_LAYOUT_V2
    av_channel_layout_uninit(&resampler->target_channel_layout);
#endif

    free(resampler);
}

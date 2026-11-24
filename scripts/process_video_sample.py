"""Local-only video sample checks, timestamped frames and ASR. Never downloads models."""
import argparse
from array import array
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave


def write_json(path, data):
    with path.open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class ProcessingLimit(Exception):
    """Fixed local reason codes, safe to include in result receipts."""


def frame_at(container, stream, seconds, start):
    target = seconds + start
    container.seek(max(0, int(target / stream.time_base)), stream=stream, backward=True)
    previous = None
    for frame in container.decode(stream):
        if frame.time is None:
            continue
        previous = frame
        if frame.time >= target:
            return frame, max(0, float(frame.time) - start)
    if previous is not None:
        return previous, max(0, float(previous.time) - start)
    raise ProcessingLimit('frame_not_decodable')


def timestamp(seconds, sep=','):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02}{sep}{millis:03}'


def process(source, output, model_dir=None, skip_asr=False, source_label=None):
    import av
    from PIL import Image
    source = source.resolve(strict=True)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sha = hashlib.sha256()
    with source.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            sha.update(chunk)
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'source_file': str(source),
              'source_sha256': sha.hexdigest(), 'source_label': source_label,
              'identity_verification': 'pending_title_author_and_visual_check',
              'decode': 'not_started', 'transcription': 'not_started', 'frames': [],
              'human_audio_spotcheck': 'pending', 'status': 'partial'}
    try:
        with av.open(str(source)) as container:
            result['audio_streams'] = len(container.streams.audio)
            if not container.streams.video:
                raise ProcessingLimit('audio_without_video' if container.streams.audio else 'no_video_or_audio_stream')
            video = container.streams.video[0]
            start = float(video.start_time * video.time_base) if video.start_time is not None else 0.0
            duration = float(video.duration * video.time_base) if video.duration else float(container.duration or 0)/av.time_base
            if duration <= 0:
                raise ProcessingLimit('unknown_duration')
            result.update(duration_s=duration, width=video.codec_context.width,
                          height=video.codec_context.height, audio_streams=len(container.streams.audio))
            frame_dir = output/'frames'
            frame_dir.mkdir()
            for index, fraction in enumerate((0, .25, .5, .75, .9), 1):
                frame, actual = frame_at(container, video, duration*fraction, start)
                path = frame_dir/f'{index:02}_{actual:09.3f}s.jpg'
                frame.to_image().save(path, 'JPEG', quality=92)
                with Image.open(path) as im:
                    im.verify()
                result['frames'].append({'file': str(path), 'requested_s': duration*fraction,
                                         'actual_s': actual})
            for position in (0, duration*.5, max(0, duration-.5)):
                frame_at(container, video, position, start)
            result['decode'] = 'beginning_middle_end_video_decodable'
            if container.streams.audio:
                # Check real audio frames rather than merely the presence of an audio track.
                with av.open(str(source)) as audio_container:
                    first = next(audio_container.decode(audio=0), None)
                    result['audio_decode'] = 'decodable' if first is not None else 'no_decodable_audio'
            else:
                result['audio_decode'] = 'no_audio_stream'
        if result['audio_decode'] != 'decodable':
            result['transcription'] = 'not_applicable_without_decodable_audio'
        elif skip_asr:
            result['transcription'] = 'skipped_explicitly'
        elif not model_dir or not model_dir.is_dir() or not (model_dir/'model.bin').is_file():
            result['transcription'] = 'local_model_unavailable_no_download'
        else:
            os.environ['HF_HUB_OFFLINE'] = '1'
            os.environ['TRANSFORMERS_OFFLINE'] = '1'
            from faster_whisper import WhisperModel
            model = WhisperModel(str(model_dir.resolve()), device='cpu', compute_type='int8', local_files_only=True)
            result['local_model_loaded'] = True
            segments, info = model.transcribe(str(source), language='zh', beam_size=5,
                                              vad_filter=True, word_timestamps=True)
            rows = []
            for segment in segments:
                reasons = []
                if segment.avg_logprob < -1: reasons.append('low_log_probability')
                if segment.no_speech_prob > .6: reasons.append('possible_non_speech')
                if segment.compression_ratio > 2.4: reasons.append('possible_repetition')
                rows.append({'start_s': segment.start, 'end_s': segment.end,
                             'text': segment.text.strip(), 'review_required': bool(reasons),
                             'listening_status': 'unreviewed',
                             'review_reasons': reasons,
                             'avg_logprob': segment.avg_logprob,
                             'no_speech_prob': segment.no_speech_prob})
            write_json(output/'transcript.json', {'kind': 'spoken_transcript_not_original_script',
                       'engine': 'faster-whisper', 'model': str(model_dir.resolve()),
                       'language': info.language, 'segments': rows})
            with (output/'口播转写.txt').open('x', encoding='utf-8') as txt, (output/'transcript.srt').open('x', encoding='utf-8') as srt:
                txt.write('口播转写，非原始创作稿；全文为待复听机器转写，低置信片段另标[需复听]。\n\n')
                for i, row in enumerate(rows, 1):
                    text = ('[需复听] ' if row['review_required'] else '') + row['text']
                    txt.write(f"[{timestamp(row['start_s'], '.')}–{timestamp(row['end_s'], '.')}] {text}\n")
                    srt.write(f"{i}\n{timestamp(row['start_s'])} --> {timestamp(row['end_s'])}\n{text}\n\n")
            result['transcription'] = 'generated_pending_listening_review' if rows else 'no_speech_detected'
            result['segment_count'] = len(rows)
        if result['audio_decode'] == 'decodable':
            import imageio_ffmpeg
            audio_dir = output/'audio_spotchecks'
            audio_dir.mkdir()
            result['spotchecks'] = []
            length = min(10.0, result['duration_s'])
            for i, start_s in enumerate((0, max(0, (duration-length)/2), max(0, duration-length)), 1):
                path = audio_dir/f'{i:02}_{start_s:09.3f}s.wav'
                run = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner',
                                      '-loglevel', 'error', '-n', '-ss', str(start_s), '-i', str(source),
                                      '-t', str(length), '-vn', '-ar', '16000', '-ac', '1', str(path)],
                                     capture_output=True, timeout=90)
                valid = False
                actual_length = 0.0
                signal = 'not_checked'
                if run.returncode == 0 and path.is_file():
                    try:
                        with wave.open(str(path), 'rb') as wav:
                            valid = wav.getnframes() > 0
                            actual_length = wav.getnframes()/wav.getframerate()
                            samples = array('h', wav.readframes(wav.getnframes()))
                            signal = 'nonzero_audio' if any(samples) else 'digital_silence'
                    except (wave.Error, EOFError):
                        pass
                result['spotchecks'].append({'start_s': start_s, 'requested_duration_s': length,
                                            'duration_s': actual_length, 'signal': signal,
                                            'file': str(path), 'created': valid})
        ready = (result['transcription'] == 'generated_pending_listening_review'
                 and len(result.get('spotchecks', [])) == 3
                 and all(s['created'] for s in result['spotchecks']))
        result['status'] = 'artifacts_ready_pending_identity_and_audio_review' if ready else 'partial'
    except ProcessingLimit as exc:
        result['reason'] = str(exc)
        result['status'] = 'partial_or_failed'
    except Exception as exc:
        # Only class names are retained; upstream error text may include private paths or URL parameters.
        result['error_type'] = type(exc).__name__
        result['status'] = 'partial_or_failed'
    write_json(output/'processing-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--skip-asr', action='store_true')
    parser.add_argument('--source-label')
    args = parser.parse_args()
    result = process(args.video, args.output, args.model_dir, args.skip_asr, args.source_label)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status']=='artifacts_ready_pending_identity_and_audio_review' else 2


if __name__ == '__main__':
    sys.exit(main())

"""Explicit local faster-whisper runner. Does not download models or call APIs."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    model_path = Path(args.model)
    if not all((model_path/name).is_file() for name in ('model.bin','config.json','tokenizer.json')):
        raise SystemExit('Local model files missing')
    from faster_whisper import WhisperModel
    model = WhisperModel(str(model_path.resolve()), device='cpu', compute_type='int8', cpu_threads=4,
                         local_files_only=True)
    segments, _ = model.transcribe(args.input, language='zh', beam_size=5, vad_filter=True,
                                  condition_on_previous_text=False)
    speech = [{'start':s.start, 'end':s.end, 'text':s.text} for s in segments]
    Path(args.output).write_text(json.dumps({'speech':speech},ensure_ascii=False),encoding='utf-8')


if __name__ == '__main__':
    main()

"""Per-run directories and exclusive output creation; no browser access."""
from datetime import datetime
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
from typing import NamedTuple
import uuid


class RunPaths(NamedTuple):
    output_dir: str
    download_dir: str
    run_id: str
    platform: str

    def env(self):
        """Environment passed to this run's child processes."""
        return {'MEDIAEXPORT_OUTPUT_DIR': self.output_dir,
                'MEDIAEXPORT_DOWNLOAD_DIR': self.download_dir,
                'MEDIAEXPORT_RUN_ID': self.run_id,
                'MEDIAEXPORT_RUN_PLATFORM': self.platform}


def start_run(platform, output_root, downloads_root):
    """Always allocate a new pair, including retries started as a new run."""
    if platform not in ('douyin', 'xhs', 'wechat'):
        raise ValueError('Unknown export platform')
    run_id = platform + '_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex
    out = Path(output_root).resolve() / run_id
    downloads = Path(downloads_root).resolve() / run_id
    out.parent.mkdir(parents=True, exist_ok=True)
    downloads.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(exist_ok=False)
    downloads.mkdir(exist_ok=False)
    paths = RunPaths(str(out), str(downloads), run_id, platform)
    with (out / '.run.json').open('x', encoding='utf-8') as handle:
        json.dump(paths._asdict(), handle, ensure_ascii=False)
    return paths


def child_run(platform, output_root, downloads_root, env=None):
    """Reuse a verified parent output; give every child its own downloads."""
    env = os.environ if env is None else env
    parent_id = env.get('MEDIAEXPORT_PARENT_RUN')
    if not parent_id:
        return start_run(platform, output_root, downloads_root)
    out = Path(env['MEDIAEXPORT_OUTPUT_DIR']).resolve()
    downloads = Path(env['MEDIAEXPORT_DOWNLOAD_DIR']).resolve()
    with (out / '.run.json').open(encoding='utf-8') as handle:
        parent = json.load(handle)
    if (parent_id != env.get('MEDIAEXPORT_RUN_ID') or parent_id != parent.get('run_id')
            or platform != parent.get('platform')
            or platform != env.get('MEDIAEXPORT_RUN_PLATFORM')
            or str(out) != parent.get('output_dir')
            or str(downloads) != parent.get('download_dir')):
        raise ValueError('Parent export context mismatch; refusing to reuse directories')
    step = downloads / ('step_' + uuid.uuid4().hex)
    step.mkdir(exist_ok=False)
    return RunPaths(str(out), str(step), parent_id, platform)


@contextmanager
def exclusive_output(destination, mode='xb', **kwargs):
    """Publish only a closed, flushed file; retain interrupted writes as .partial.

    Windows rename refuses an existing target, unlike os.replace. The POSIX
    fallback uses an exclusive hard link and retains its staging name.
    """
    destination = Path(destination)
    partial = destination.with_name(destination.name + '.partial-' + uuid.uuid4().hex)
    with partial.open(mode, **kwargs) as writer:
        yield writer
        writer.flush()
        os.fsync(writer.fileno())
    if os.name == 'nt':
        os.rename(partial, destination)
    else:
        os.link(partial, destination)


def copy_exclusive(source, destination):
    """Copy and atomically publish without overwriting any existing output."""
    with open(source, 'rb') as reader, exclusive_output(destination) as writer:
        shutil.copyfileobj(reader, writer)

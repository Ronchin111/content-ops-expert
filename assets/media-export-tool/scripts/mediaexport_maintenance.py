"""Local maintenance interlock. No browser, configuration or account access."""
from pathlib import Path
import sys
import argparse
import uuid


def maintenance_active():
    """Return observed marker state, never treat access failure as available."""
    try:
        MARKER.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MaintenanceBlocked("无法确认维护状态，已停止取数。") from exc

MARKER = Path(__file__).resolve().parent.parent / ".mediaexport-maintenance"
MESSAGE = "工具正在维护，暂不接受取数任务。请等待维护完成后再试。"


class MaintenanceBlocked(RuntimeError):
    """The entrypoint must not start an export."""


def require_available():
    """Read the marker on every call; unreadable state fails closed."""
    if maintenance_active():
        raise MaintenanceBlocked(MESSAGE)


def check_start():
    """Stop direct entrypoints before loading browser dependencies."""
    try:
        require_available()
    except MaintenanceBlocked as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(78) from None


def main():
    """Explicit local operations; marker removal is a recoverable rename."""
    parser = argparse.ArgumentParser(description='Local maintenance control')
    parser.add_argument('action', choices=['enable', 'disable', 'status'], nargs='?')
    parser.add_argument('--confirm-root')
    args = parser.parse_args()
    if args.action is None:
        check_start()
        return 0
    try:
        if args.action != 'status':
            if not args.confirm_root or Path(args.confirm_root).resolve() != MARKER.parent:
                raise ValueError('必须用 --confirm-root 明确确认本工具根目录。')
        if args.action == 'enable' and not maintenance_active():
            with MARKER.open('x', encoding='utf-8') as handle:
                handle.write('MediaExport maintenance\n')
        if args.action == 'disable' and maintenance_active():
            if MARKER.is_symlink() or not MARKER.is_file():
                raise ValueError('标记不是普通文件，请核对，不自动解除。')
            archived = MARKER.with_name(MARKER.name + '.released-' + str(uuid.uuid4()))
            MARKER.rename(archived)
            print('已保留解除记录：' + str(archived))
        active = maintenance_active()
        print('工具根目录=' + str(MARKER.parent))
        print('当前状态=' + ('维护中' if active else '可运行'))
        if args.action == 'enable' and not active or args.action == 'disable' and active:
            raise ValueError('状态回读不符合操作预期，请停止后续步骤。')
        return 0
    except (OSError, ValueError, MaintenanceBlocked) as exc:
        print('维护操作失败：' + str(exc), file=sys.stderr)
        return 79


if __name__ == '__main__':
    sys.exit(main())

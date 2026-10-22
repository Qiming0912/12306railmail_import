"""Read bytes without extracting ZIP entries to disk."""
from pathlib import Path
import zipfile


def iter_sources(paths, max_mail_bytes=20 * 1024 * 1024):
    """Yield (source, raw, error); continue after individual file failures."""
    seen = set()
    for name in paths:
        path = Path(name)
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        for file in files:
            if file.is_dir():
                continue
            if file.suffix.lower() not in ('.eml', '.zip') and path.is_dir():
                continue
            source = str(file.resolve())
            if source in seen:
                continue
            seen.add(source)
            try:
                if file.suffix.lower() == '.eml':
                    if file.stat().st_size > max_mail_bytes:
                        raise ValueError('EML exceeds size limit')
                    yield source, file.read_bytes(), None
                elif file.suffix.lower() == '.zip':
                    with zipfile.ZipFile(file) as archive:
                        for entry in archive.infolist():
                            if entry.is_dir() or not entry.filename.lower().endswith('.eml'):
                                continue
                            origin = source + '!' + entry.filename
                            try:
                                if entry.file_size > max_mail_bytes:
                                    raise ValueError('EML exceeds size limit')
                                yield origin, archive.read(entry), None
                            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                                yield origin, None, str(exc)
                else:
                    raise ValueError('Expected EML, ZIP or directory')
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                yield source, None, str(exc)

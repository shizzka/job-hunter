"""Advisory analysis publication bound to original resume/output bytes."""
from pathlib import Path

from .json_store import atomic_write_text, file_lock


def analysis_succeeded(text):
    return isinstance(text, str) and bool(text.strip()) and not text.startswith((
        'Ошибка анализа', 'Резюме пустое', 'Файл не найден', '(пустой ответ LLM)'))


def _read_optional(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


class AnalysisPublication:
    def __init__(self, resume_file, analysis_file):
        self.resume = Path(resume_file).absolute()
        self.analysis = Path(analysis_file).absolute()
        if self.resume == self.analysis:
            raise ValueError('Analysis cannot replace the source resume')
        with file_lock(self.resume), file_lock(self.analysis):
            self.original_resume = self.resume.read_bytes()
            self.original_analysis = _read_optional(self.analysis)

    def publish(self, text):
        if not analysis_succeeded(text):
            return False
        with file_lock(self.resume), file_lock(self.analysis):
            if (self.resume.read_bytes() != self.original_resume
                    or _read_optional(self.analysis) != self.original_analysis):
                raise RuntimeError('Resume/analysis changed; stale analysis cannot replace it')
            atomic_write_text(self.analysis, text)
        return True

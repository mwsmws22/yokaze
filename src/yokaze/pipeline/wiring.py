"""Build the default port graph (all native stages)."""

from __future__ import annotations

from yokaze.lib.capture import Capture
from yokaze.lib.crop.opencv_crop import OpenCvCrop
from yokaze.lib.deliver import Deliver
from yokaze.lib.tone import Finish
from yokaze.pipeline.document import DocumentPipeline
from yokaze.lib.brod import Preprocess


def default_pipeline() -> DocumentPipeline:
    return DocumentPipeline(
        capture=Capture(),
        preprocess=Preprocess(),
        crop=OpenCvCrop(),
        finish=Finish(),
        deliver=Deliver(),
    )

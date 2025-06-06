import logging
from typing import Iterable

from docling_core.types.doc import BoundingBox, CoordOrigin

from docling.datamodel.base_models import Cell, OcrCell, Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import TesseractOcrOptions
from docling.datamodel.settings import settings
from docling.models.base_ocr_model import BaseOcrModel
from docling.utils.ocr_utils import map_tesseract_script
from docling.utils.profiling import TimeRecorder

_log = logging.getLogger(__name__)


class TesseractOcrModel(BaseOcrModel):
    def __init__(self, enabled: bool, options: TesseractOcrOptions):
        super().__init__(enabled=enabled, options=options)
        self.options: TesseractOcrOptions

        self.scale = 3  # multiplier for 72 dpi == 216 dpi.
        self.reader = None
        self.osd_reader = None

        if self.enabled:
            install_errmsg = (
                "tesserocr is not correctly installed. "
                "Please install it via `pip install tesserocr` to use this OCR engine. "
                "Note that tesserocr might have to be manually compiled for working with "
                "your Tesseract installation. The Docling documentation provides examples for it. "
                "Alternatively, Docling has support for other OCR engines. See the documentation: "
                "https://ds4sd.github.io/docling/installation/"
            )
            missing_langs_errmsg = (
                "tesserocr is not correctly configured. No language models have been detected. "
                "Please ensure that the TESSDATA_PREFIX envvar points to tesseract languages dir. "
                "You can find more information how to setup other OCR engines in Docling "
                "documentation: "
                "https://ds4sd.github.io/docling/installation/"
            )

            try:
                import tesserocr
            except ImportError:
                raise ImportError(install_errmsg)
            try:
                tesseract_version = tesserocr.tesseract_version()
            except:
                raise ImportError(install_errmsg)

            _, self._tesserocr_languages = tesserocr.get_languages()
            if not self._tesserocr_languages:
                raise ImportError(missing_langs_errmsg)

            # Initialize the tesseractAPI
            _log.debug("Initializing TesserOCR: %s", tesseract_version)
            lang = "+".join(self.options.lang)

            if lang != "auto":
                for lg in self.options.lang:
                    if lg in ("jpn", "jpn_vert"):
                        # Check if the language or its script prefixed version is available
                        lang_available = lg in self._tesserocr_languages
                        if self.script_prefix:
                            lang_available = lang_available or f"{self.script_prefix}{lg}" in self._tesserocr_languages

                        if not lang_available:
                            _log.warning(
                                f"The requested Tesseract language '{lg}' for Japanese is not installed. "
                                f"Please install the appropriate Tesseract language data (e.g., 'jpn.traineddata', 'jpn_vert.traineddata'). "
                                f"You can usually find installation instructions at https://tesseract-ocr.github.io/tessdoc/Data-Files.html "
                                f"or by searching for 'install tesseract language packs' for your operating system."
                            )

            self.script_readers: dict[str, tesserocr.PyTessBaseAPI] = {}

            if any([l.startswith("script/") for l in self._tesserocr_languages]):
                self.script_prefix = "script/"
            else:
                self.script_prefix = ""

            tesserocr_kwargs = {
                "psm": tesserocr.PSM.AUTO,
                "init": True,
                "oem": tesserocr.OEM.DEFAULT,
            }

            if self.options.path is not None:
                tesserocr_kwargs["path"] = self.options.path

            if lang == "auto":
                self.reader = tesserocr.PyTessBaseAPI(**tesserocr_kwargs)
                self.osd_reader = tesserocr.PyTessBaseAPI(
                    **{"lang": "osd", "psm": tesserocr.PSM.OSD_ONLY} | tesserocr_kwargs
                )
            else:
                self.reader = tesserocr.PyTessBaseAPI(
                    **{"lang": lang} | tesserocr_kwargs,
                )
            self.reader_RIL = tesserocr.RIL

    def __del__(self):
        if self.reader is not None:
            # Finalize the tesseractAPI
            self.reader.End()
        for script in self.script_readers:
            self.script_readers[script].End()

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:
        if not self.enabled:
            yield from page_batch
            return

        for page in page_batch:
            assert page._backend is not None
            if not page._backend.is_valid():
                yield page
            else:
                with TimeRecorder(conv_res, "ocr"):
                    assert self.reader is not None
                    assert self._tesserocr_languages is not None

                    ocr_rects = self.get_ocr_rects(page)

                    all_ocr_cells = []
                    for ocr_rect in ocr_rects:
                        # Skip zero area boxes
                        if ocr_rect.area() == 0:
                            continue
                        high_res_image = page._backend.get_page_image(
                            scale=self.scale, cropbox=ocr_rect
                        )

                        local_reader = self.reader
                        if "auto" in self.options.lang:
                            assert self.osd_reader is not None

                            self.osd_reader.SetImage(high_res_image)
                            osd = self.osd_reader.DetectOrientationScript()

                            # No text, probably
                            if osd is None:
                                continue

                            osd_script_name = osd["script_name"]
                            script = map_tesseract_script(osd["script_name"])

                            tess_lang_to_use = f"{self.script_prefix}{script}"
                            tess_lang_to_use_vert = None

                            if script == "Japanese" and osd.get("orientation_degrees") is not None:
                                if osd["orientation_degrees"] == 90 or osd["orientation_degrees"] == 270:
                                    # Check for vertical script names
                                    possible_vert_names = [f"{self.script_prefix}Japanese_vert", f"{self.script_prefix}jpn_vert"]
                                    for vert_name in possible_vert_names:
                                        if vert_name in self._tesserocr_languages:
                                            tess_lang_to_use_vert = vert_name
                                            tess_lang_to_use = vert_name  # Prioritize vertical if found
                                            break

                            lang_to_check = tess_lang_to_use_vert if tess_lang_to_use_vert else tess_lang_to_use

                            # Check if the detected language is present in the system
                            if lang_to_check not in self._tesserocr_languages:
                                msg = f"Tesseract OSD detected script '{osd_script_name}' (mapped to '{script}')."
                                if tess_lang_to_use_vert:
                                    msg += f" Orientation suggested vertical text, attempted to use '{tess_lang_to_use_vert}'."
                                msg += f" However, the required Tesseract language '{lang_to_check}' (or its vertical variant) is not installed or found in {self._tesserocr_languages}."
                                msg += " Please install the appropriate Tesseract language data (e.g., 'jpn.traineddata', 'jpn_vert.traineddata' for Japanese). You can usually find installation instructions at https://tesseract-ocr.github.io/tessdoc/Data-Files.html or by searching for 'install tesseract language packs' for your operating system. The script will be ignored for OCR."
                                _log.warning(msg)
                            else:
                                if script not in self.script_readers or (tess_lang_to_use_vert and self.script_readers.get(script) and self.script_readers[script].GetLanguage() != tess_lang_to_use_vert) : # Re-init if orientation changed
                                    import tesserocr

                                    self.script_readers[script] = (
                                        tesserocr.PyTessBaseAPI(
                                            path=self.reader.GetDatapath(),
                                            lang=tess_lang_to_use, # Use the determined language (could be vertical)
                                            psm=tesserocr.PSM.AUTO,
                                            init=True,
                                            oem=tesserocr.OEM.DEFAULT,
                                        )
                                    )
                                local_reader = self.script_readers[script]

                        local_reader.SetImage(high_res_image)
                        boxes = local_reader.GetComponentImages(
                            self.reader_RIL.TEXTLINE, True
                        )

                        cells = []
                        for ix, (im, box, _, _) in enumerate(boxes):
                            # Set the area of interest. Tesseract uses Bottom-Left for the origin
                            local_reader.SetRectangle(
                                box["x"], box["y"], box["w"], box["h"]
                            )

                            # Extract text within the bounding box
                            text = local_reader.GetUTF8Text().strip()
                            confidence = local_reader.MeanTextConf()
                            left = box["x"] / self.scale
                            bottom = box["y"] / self.scale
                            right = (box["x"] + box["w"]) / self.scale
                            top = (box["y"] + box["h"]) / self.scale

                            cells.append(
                                OcrCell(
                                    id=ix,
                                    text=text,
                                    confidence=confidence,
                                    bbox=BoundingBox.from_tuple(
                                        coord=(left, top, right, bottom),
                                        origin=CoordOrigin.TOPLEFT,
                                    ),
                                )
                            )

                        # del high_res_image
                        all_ocr_cells.extend(cells)

                    # Post-process the cells
                    page.cells = self.post_process_cells(all_ocr_cells, page.cells)

                # DEBUG code:
                if settings.debug.visualize_ocr:
                    self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)

                yield page

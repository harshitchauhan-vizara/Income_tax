from io import BytesIO
import logging

from ..utils.language_detector import map_tts_lang

logger = logging.getLogger("app.tts.gtts_service")


class GTTSService:
    def synthesize(self, text: str, language: str = "en") -> bytes:
        try:
            from gtts import gTTS

            tts = gTTS(text=text, lang=map_tts_lang(language), slow=False)
            output = BytesIO()
            tts.write_to_fp(output)
            return output.getvalue()
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("TTS synthesis failed: %s", exc)
            return b""

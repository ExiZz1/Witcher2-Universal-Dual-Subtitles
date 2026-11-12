"""Locale resources and stock-renderer policy, separate from text merging."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Locale:
    name: str
    stem: str
    font_profile: str = "default"
    codec_verified: bool = True

    @property
    def filename(self):
        return self.stem + ".w2strings"

    @property
    def cjk(self):
        return self.font_profile != "default"


LOCALES = {locale.name: locale for locale in (
    Locale("English", "en0"),
    Locale("Polish", "pl0"),
    Locale("Russian", "ru0"),
    Locale("German", "de0"),
    Locale("French", "fr0"),
    Locale("Spanish", "es0"),
    Locale("Italian", "it0"),
    Locale("Czech", "cz0"),
    Locale("Hungarian", "hu0"),
    Locale("Japanese", "jp0", "jp"),
    Locale("Turkish", "tr0"),
    Locale("Chinese", "zh0", "zh"),
)}
LANGS = {name: locale.filename for name, locale in LOCALES.items() if locale.codec_verified}


def target_locale(primary, secondary):
    # Keep the active Primary profile where it can serve as a target. When
    # only Secondary is CJK, use its stock profile and keep the text order.
    return secondary if secondary.cjk and not primary.cjk else primary


def renderer_restriction(primary, secondary):
    if primary.cjk and secondary.cjk:
        return "CJK + CJK passed offline codec testing. The stock renderer and mixed glyph coverage are not verified in game."
    if "tr0" in (primary.stem, secondary.stem) and (primary.cjk or secondary.cjk):
        return "Turkish + CJK passed offline codec testing. The stock CJK fonts lack required Turkish glyphs."
    return None


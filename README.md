# The Witcher 2 Universal Dual Subtitles v3.0

**Created by ExiZz**

Show two text languages together in The Witcher 2: Assassins of Kings Enhanced Edition. Primary appears first and determines the game's Text Language setting. Secondary follows wherever both localizations provide eligible text.

**Supported text languages:** English, Polish, Russian, German, French, Spanish, Italian, Czech, Hungarian, Japanese, Turkish and Traditional Chinese.

## Install

1. Extract the entire release ZIP. Keep the EXE next to its `_internal` folder.
2. Close the game. Run `Witcher2_Universal_Dual_Subtitles.exe`.
3. Check the detected game path or use **Browse** to select the game directory containing `CookedPC`.
4. Select two different available languages and click **Install**. Confirm the localization files only if you know they are clean game files. If unsure, verify them through your game store first.
5. **Set the game's Text Language to your selected Primary language.** Audio language is separate.

The portable application supports Windows 10 22H2 and Windows 11, x64. It includes Python, Tcl/Tk and its native dependencies. The included Gibbed tools use the standard Windows .NET Framework 4.x. Install and Restore need write access to the game directory.

## Restore or change languages

Close the game, open the tool on the same game folder and click **Restore**. It uses the active installation's journal and restores the original bytes. Keep `CookedPC/_ExiZz_DualSubtitles_State` while the mod is active. To change languages, complete Restore, select the new pair and click Install.

If the tool reports an external change or untracked font override, follow the log before retrying. Preserve the state folder until Restore succeeds.

## Fonts and compatibility

The app selects the font profile from Primary. It fills missing glyphs from supported stock font resources or the licensed Noto catalog. Chinese profiles include a readability adjustment. Shared Han characters use the Primary font's regional form when Japanese and Chinese forms differ.

Game resources are checked structurally. A compatible revision can be used even if its checksum changes. The application may ask you to reconfirm changed original localizations. Hashes protect backups, commits and Restore. Unsupported or conflicting resources are rejected before replacement.

All 132 ordered language pairs passed offline checks. Polish/Japanese, Turkish/Japanese, English/Chinese and Turkish/Chinese were checked in both directions in game during development. Some records have missing counterparts, exceptional characters or unsupported glyphs and stay original. The operation log shows the coverage report path. Very long combined lines can still clip inside game text fields.

## Credits

Created by **ExiZz**. The Witcher 2 is a game by CD PROJEKT RED. The app reads the emblem from the local game executable when one is available. Third-party terms are in `Licenses/` and `THIRD-PARTY-NOTICES.txt`.

## Source and builds

See [BUILD_GUIDE.md](BUILD_GUIDE.md) for the pinned Windows build, dependency staging and test commands. See [CHANGELOG.md](CHANGELOG.md) for release history. Game assets come from your game installation or test copies supplied by the tester.

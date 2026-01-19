# Mixxx to Rekordbox XML Converter

This utility allows DJs to migrate their library metadata, crates, and
playlists from **Mixxx** to **Rekordbox**. It parses the Mixxx SQLite
database and generates a `rekordbox.xml` file that can be imported
into Rekordbox as an external library. It exports track metadata
(Artist, Title, Album, BPM, Year, Genre, Grouping, and Comment) as
well as converts Mixxx cue points into Rekordbox' memory
cues. Unfortunately, due to the way rekordbox implements mp3 decoding,
the timing of the memory cues sometimes can be off by some 10-50 ms
which while not perfect remains accurate enough for most purposes.

# Usage

By default `mixxx2rekordbox.py` will try to export all crates from
mixxx into a `rekordbox.xml` file. You can specify location of the
database and the output file on the command line

```bash
mixxx2rekordbox.py ~/.mixxx/mixxxdb.sqlite -o rekordbox.xml
```
or in the configuration file `.mixxx2rekordbox`, which should be
either in the current or in the home directory. The tracks in the
crate can be sorted by BPM during export
```bash
mixxx2rekordbox.py --sort-by-bpm asc
```

With
```bash
mixxx2rekordbox.py -p my_playlist1,myplaylist2
```
you can export individual playlists (the tracks order will be the same
as in the playlist).

With
```bash
mixxx2rekordbox.py -e incoming,trash,demos
```
you can exclude some crates from the export. All these options can be
specified in `.mixxx2rekordbox`. Additionally you can add 
```bash
default_playlists = bangers1,perfect_opening
```
to the configuration file to specify which playlists are exported by
default when the option `-p` is used without arguments:
With
```bash
mixxx2rekordbox.py -p
```

With `--list-playlists` and `--list-crates` you can get lists of
playlists and crates in your mixxx database.

The configuration file supports `~` which points to the home directory.

## Importing to Rekordbox

1. * Open Rekordbox and go to **Preferences > Advanced > Database**.
2. * In the **rekordbox xml** section, browse and select your generated `rekordbox.xml`.
3. * In the Rekordbox tree view (sidebar), scroll down to the **rekordbox xml** section
4. * Expand the node to see your exported collections.
5. * Right-click a playlist or crate and select **Import to Collection**. This will copy the tracks and their cue points into your main Rekordbox database.


#!/usr/bin/env python3


import argparse
import configparser
import math
import os
import sqlite3
import struct
import sys
from pathlib import Path
from urllib.parse import quote
from xml.dom import minidom
from xml.etree import ElementTree as ET

# import beats_pb2 as pb2


def get_db_connection(db_file):
    """Establishes a read-only connection to the SQLite database."""
    try:
        normalized_path = os.path.abspath(os.path.expanduser(db_file))
        conn = sqlite3.connect(f"file:{normalized_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        sys.exit(1)

def load_config():
    """Loads configuration from .mixxx2rekordbox in current or home directory."""
    config = configparser.ConfigParser()
    files_to_check = [Path.cwd() / ".mixxx2rekordbox", Path.home() / ".mixxx2rekordbox"]
    
    conf_dict = {}
    for config_path in files_to_check:
        if config_path.exists():
            config.read(config_path)
            if 'default' in config:
                conf_dict = dict(config['default'])
                # Expand ~ paths in config
                for key in ['db_path', 'output_path']:
                    if key in conf_dict:
                        conf_dict[key] = os.path.expanduser(conf_dict[key])
            break
    return conf_dict

def get_collections(conn, include_names=None, exclude_names=None, mode='playlists,crates'):
    """Fetches crates or playlists or both from the database."""

    collection_data = {}
    all_track_ids = set()

    table_map = {
        'crates': ('crates', 'crate_tracks', 'crate_id'),
        'playlists': ('Playlists', 'PlaylistTracks', 'playlist_id')
    }

    modes = mode.split(',')


    for mode in modes:
        main_table, link_table, fk_id = table_map[mode]
        specific_collection, subset_track_ids  = get_specific_collection(conn, mode, include_names, exclude_names, main_table, link_table, fk_id)
        collection_data.update(specific_collection)
        all_track_ids.update(subset_track_ids)


    return collection_data, all_track_ids

def get_specific_collection(conn, mode, include_names, exclude_names, main_table, link_table, fk_id):
    """Fetches either crates or playlists from the database depending on the mode."""
    cursor = conn.cursor()
    all_db_items = {}

    try:
        query = f"SELECT id, name FROM {main_table}"
        if mode == 'playlists':
            query += " WHERE hidden = 0"
        
        cursor.execute(query)
        for row in cursor.fetchall():
            all_db_items[row['name']] = row['id']
    except sqlite3.Error as e:
        print(f"Error fetching {mode}: {e}", file=sys.stderr)
        return {}, set()

    if include_names is not None:
        final_names = [n for n in include_names if n in all_db_items]
    elif exclude_names:
        final_names = [name for name in all_db_items if name not in exclude_names]
    else:
        final_names = list(all_db_items.keys())
    
    if not final_names:
        return {}, set()

    collection_data = {name: {'id': all_db_items[name], 'tracks': []} for name in final_names}
    item_ids = [c['id'] for c in collection_data.values()]
    placeholders = ', '.join('?' for _ in item_ids)
    
    tracks_query = f"SELECT {fk_id}, track_id FROM {link_table} WHERE {fk_id} IN ({placeholders})"
    if mode == 'playlists':
        tracks_query += " ORDER BY position ASC"
    
    all_track_ids = set()
    try:
        cursor.execute(tracks_query, item_ids)
        id_to_name = {v['id']: k for k, v in collection_data.items()}
        for row in cursor.fetchall():
            name = id_to_name.get(row[fk_id])
            if name:
                collection_data[name]['tracks'].append(row['track_id'])
                all_track_ids.add(row['track_id'])
    except sqlite3.Error as e:
        print(f"Error fetching {mode} tracks: {e}", file=sys.stderr)

    return collection_data, all_track_ids

def get_track_details(conn, track_ids):
    """Fetches metadata from 'library' and 'track_locations'."""
    if not track_ids: return {}
    track_details = {}
    placeholders = ', '.join('?' for _ in track_ids)

    query = f"""
        SELECT l.id, l.artist, l.title, l.album, l.year, l.genre, l.grouping,
               l.tracknumber, l.comment, l.samplerate, l.bitrate, l.bpm,
               l.datetime_added, l.duration, tl.location, tl.filesize,
               l.filetype, l.key_id, l.beats, l.beats_version, l.rating
        FROM library l 
        JOIN track_locations tl ON l.location = tl.id
        WHERE l.id IN ({placeholders})
    """
    cues_query = f"SELECT track_id, position, type, length, hotcue, label, color FROM cues WHERE track_id IN ({placeholders})"

    cursor = conn.cursor()
    try:
        cursor.execute(query, list(track_ids))
        for row in cursor.fetchall():
            track_details[row['id']] = dict(row)
            track_details[row['id']]['cues'] = []

        cursor.execute(cues_query, list(track_ids))
        for row in cursor.fetchall():
            tid = row['track_id']
            if tid in track_details:
                track_details[tid]['cues'].append({"position": row['position'], "type": row['type'], "length": row['length'], "hotcue": row['hotcue'], "label": row['label'], "color": row['color']})
    except sqlite3.Error as e:
        print(f"Error fetching track metadata: {e}", file=sys.stderr)
        
    return track_details


def _varint(buf, i):
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if b < 0x80:
            return value, i


def pb_fields(buf):
    """(field number, value) pairs of a protobuf message."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        wire = key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            n, i = _varint(buf, i)
            value, i = buf[i:i + n], i + n
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unexpected wire type {wire}")
        yield key >> 3, value


def _int32(v):
    return v - (1 << 64) if v >= 1 << 63 else v


def beat_frame(beat):
    return next((_int32(v) for f, v in pb_fields(beat) if f == 1), 0)


def read_beats(version, blob):
    """(bpm, first frame) for a grid, or a list of frames for a beat map."""
    if version == "BeatGrid-2.0":
        bpm, first = 0.0, 0
        for f, v in pb_fields(blob):
            if f == 1:  # Bpm { double bpm = 1; }
                bpm = next((struct.unpack("<d", x)[0] for g, x in pb_fields(v) if g == 1), 0.0)
            elif f == 2:  # Beat first_beat
                first = beat_frame(v)
        return bpm, first
    if version == "BeatMap-1.0":  # repeated Beat beat = 1
        return [beat_frame(v) for f, v in pb_fields(blob) if f == 1]
    if version == "BeatGrid-1.0":  # two raw doubles, as in Mixxx's fallback, not tested!
        print('This track has BeatGrid-1.0, program might fail, since this case is untested!')
        return struct.unpack("<dd", blob)
    raise ValueError(version)


def grid_tempo(bpm, first_frame, samplerate):
    """One TEMPO whose grid also covers the intro, keeping Mixxx's first beat as beat 1."""
    period = 60.0 / bpm
    first = first_frame / samplerate
    k = math.floor(first / period)
    return [(first - k * period, bpm, (-k) % 4 + 1)]


def map_tempos(frames, samplerate):
    """One TEMPO per beat, as rekordbox writes a grid whose tempo moves."""
    times = [f / samplerate for f in frames]
    tempos = []
    for i, t in enumerate(times):
        gap = times[i + 1] - t if i + 1 < len(times) else t - times[i - 1]
        tempos.append((t, 60.0 / gap, i % 4 + 1))
    return tempos


def build_xml(track_details, collections, is_playlist_mode=False, sort_order=None):
    """Builds the Rekordbox XML structure."""
    dj_playlists = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(dj_playlists, "PRODUCT", Name="rekordbox", Version="6.8.6", Company="AlphaTheta")

    # Mixxx ChromaticKey (library.key_id) -> rekordbox Alphanumeric
    KEY_ID_TO_ALPHANUMERIC = [
    None,
    "8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B",  # C, Db, D, Eb, E, F, F#, G, Ab, A, Bb, B
    "5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A",  # Cm, C#m, Dm, Ebm, Em, Fm, F#m, Gm, G#m, Am, Bbm, Bm
    ]

    # Mixxx predefined track color palette
    COLOR_MAPPING = {
            "0xFCA6D7" : "0xFF007F", #Pink
            "0xC50A08": "0xFF0000", #Red
            "0xFF8000": "0xFFA500", #Orange 
            "0xF8D200": "0xFFFF00",  #Yellow 
            "0x32BE44": "0x00FF00",  #Green 
            "0x42D4F4": "0x25FDE9",  #Aqua 
            "0x0044FF": "0x0000FF",  #Blue 
            "0xAF00CC": "0x660099",  #Purple
    }

    collection_node = ET.SubElement(dj_playlists, "COLLECTION", Entries=str(len(track_details)))
    for track_id, data in sorted(track_details.items()):
        location_url = f"file://localhost{quote(data.get('location', ''))}"
        track_attribs = {
            "TrackID": str(track_id), "Name": data.get('title') or "",
            "Artist": data.get('artist') or "", "Album": data.get('album') or "",
            "Grouping": data.get('grouping') or "", "Genre": data.get('genre') or "",
            "Year": str(data.get('year') or ""), "TrackNumber": str(data.get('tracknumber') or ""),
            "Comments": data.get('comment') or "", "Location": location_url,
            "Kind": data.get('filetype').upper() + " File" or "", "Size": str(data.get('filesize') or "0"),
            "TotalTime": str(round(data.get('duration') or 0.0)),
            "AverageBpm": f"{data.get('bpm', 0.0):.2f}",
            "BitRate": str(data.get('bitrate') or "0"), 
            "SampleRate": str(data.get('samplerate') or "0"),
            "Tonality": str(KEY_ID_TO_ALPHANUMERIC[int(data.get('key_id'))] or ""),
            "Rating": str(data.get('rating') * 51 or "0"),
        }
        track_node = ET.SubElement(collection_node, "TRACK", **track_attribs)

        samplerate = float(data.get('samplerate', 44100.0) or 44100.0)

        beat_info = read_beats(data.get('beats_version'), data.get('beats'))
        tempos = None

        if (type(beat_info) is tuple):
            tempos = grid_tempo(beat_info[0], beat_info[1], samplerate)
        elif (type(beat_info) is list):
            tempos = map_tempos(beat_info, samplerate)


        for tempo in tempos:
            ET.SubElement(track_node, "TEMPO", Inizio=f"{tempo[0]:.3f}", Bpm=f"{tempo[1]:.2f}", Metro="4/4", Battito=f"{tempo[2]}")

        for d in data.get('cues', {}):
            cue_name = d.get('label')
            cue_type = d.get('type')
            pos = d.get('position')
            cue_length = d.get('length')
            cue_start = (pos / 2) / samplerate
            cue_end = (((pos + cue_length) / 2) / samplerate)
            cue_rgb = d.get('color')
            cue_number = d.get('hotcue')
            cue_r = cue_rgb & 255
            cue_g = (cue_rgb >> 8) & 255
            cue_b = (cue_rgb >> 16) & 255

            if (cue_type == 8): # internal mixxx cue
                continue
            elif (cue_type == 0): # invalid mixxx cue
                continue
            elif (cue_type == 6): # fade-in (weird values in mixxx)
                continue
            elif (cue_type == 7): # fade-out (weird values in mixxx)
                continue
            elif (cue_type == 1 or cue_type == 2): # cues and hot cues
                cue_type = 0

            if (cue_number == -1): # skip -1 internal cues in mixxx, we will add rekordbox memory cues at same location as hotcues
                continue

            if (cue_type == 4): # loop
                ET.SubElement(track_node, "POSITION_MARK", Name=str(cue_name), Type=str(cue_type), Start=f"{cue_start:.3f}" , End=f"{cue_end:.3f}", Num="-1")
                ET.SubElement(track_node, "POSITION_MARK",  Red=str(cue_r), Green=str(cue_g), Blue=str(cue_b), Name=str(cue_name), Type=str(cue_type), Start=f"{cue_start:.3f}" , End=f"{cue_end:.3f}", Num=str(cue_number))
            else:
                ET.SubElement(track_node, "POSITION_MARK", Name=str(cue_name), Type=str(cue_type), Start=f"{cue_start:.3f}" , Num="-1")
                ET.SubElement(track_node, "POSITION_MARK", Red=str(cue_r), Green=str(cue_g), Blue=str(cue_b), Name=str(cue_name), Type=str(cue_type), Start=f"{cue_start:.3f}" , Num=str(cue_number))


    playlists_root = ET.SubElement(dj_playlists, "PLAYLISTS")
    root_node = ET.SubElement(playlists_root, "NODE", Type="0", Name="ROOT", Count=str(len(collections)))
    
    for name, data in sorted(collections.items()):
        playlist_node = ET.SubElement(root_node, "NODE", Name=name, Type="1", KeyType="0", Entries=str(len(data['tracks'])))
        track_list = data['tracks']
        
        # Only sort if we are in Crate mode and sort order is specified
        if not is_playlist_mode and sort_order:
            track_list = sorted(track_list, key=lambda tid: track_details.get(tid, {}).get('bpm', 0.0) or 0.0, reverse=(sort_order == 'desc'))
            
        for tid in track_list:
            ET.SubElement(playlist_node, "TRACK", Key=str(tid))
            
    return dj_playlists

def main():
    config = load_config()
    parser = argparse.ArgumentParser(description="Convert Mixxx data to rekordbox.xml")
    
    parser.add_argument("mixxx_db_path", nargs='?', default=config.get('db_path'), help="Path to mixxx.sqlite")
    parser.add_argument("-o", "--output", default=config.get('output_path', 'rekordbox.xml'), help="Output XML path")
    
    parser.add_argument("-p", "--playlists", nargs="*", help="Export playlists. If no names given, uses config.")
    parser.add_argument("-c", "--crates", nargs="*", help="Export crates. If no names given, exports all")
    parser.add_argument("-e", "--exclude-crates", nargs="+", 
                        default=config.get('exclude_crates', '').split(',') if config.get('exclude_crates') else [], 
                        help="Crates to exclude in default mode")
    
    parser.add_argument("--list-crates", action="store_true", help="List available crates and exit")
    parser.add_argument("--list-playlists", action="store_true", help="List available playlists and exit")
    parser.add_argument("--sort-by-bpm", choices=['asc', 'desc'], default=config.get('sort_by_bpm'), help="BPM sort order for Crates")
    
    args = parser.parse_args()
    if not args.mixxx_db_path:
        print("Error: Database path required.", file=sys.stderr)
        sys.exit(1)

    db_path = os.path.expanduser(args.mixxx_db_path)
    conn = get_db_connection(db_path)

    if args.list_crates:
        items, _ = get_collections(conn, mode='crates')
        print("Available Crates:"); [print(f" - {n}") for n in sorted(items.keys())]
        sys.exit(0)
    
    if args.list_playlists:
        items, _ = get_collections(conn, mode='playlists')
        print("Available Playlists:"); [print(f" - {n}") for n in sorted(items.keys())]
        sys.exit(0)

    is_playlist_mode = args.playlists is not None
    is_crate_mode = args.crates is not None
    if is_playlist_mode:
        target_names = args.playlists
        if not target_names:
            target_names = [p.strip() for p in config.get('default_playlists', '').split(',') if p.strip()]

        if not target_names:
            print("Error: No playlists specified via CLI or config.")
            sys.exit(1)
        collections, track_ids = get_collections(conn, include_names=target_names, mode='playlists')
    elif is_crate_mode:
        target_names = args.crates
        if not target_names:
            target_names = [p.strip() for p in config.get('default_crates', '').split(',') if p.strip()]
        if not target_names:
            print("Error: No crates specified via CLI or config.")
            sys.exit(1)
        collections, track_ids = get_collections(conn, include_names=target_names, mode='crates')

    else:
        # Default Both Mode
        collections, track_ids = get_collections(conn, exclude_names=args.exclude_crates, mode='playlists,crates')

    if not track_ids:
        print("No tracks found.")
        conn.close()
        return

    track_details = get_track_details(conn, track_ids)
    conn.close()
    
    xml_tree = build_xml(track_details, collections, is_playlist_mode, args.sort_by_bpm)
    pretty_xml = minidom.parseString(ET.tostring(xml_tree, 'utf-8')).toprettyxml(indent="  ")

    output_path = os.path.expanduser(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(pretty_xml)
    print(f"Exported to {output_path}")

if __name__ == "__main__":
    main()

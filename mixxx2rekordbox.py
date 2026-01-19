#!/usr/bin/env python3

import argparse
import sqlite3
import sys
import os
import configparser
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET
from xml.dom import minidom

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

def get_collections(conn, include_names=None, exclude_names=None, mode='crates'):
    """Fetches crates or playlists from the database."""
    cursor = conn.cursor()
    all_db_items = {}
    
    table_map = {
        'crates': ('crates', 'crate_tracks', 'crate_id'),
        'playlists': ('Playlists', 'PlaylistTracks', 'playlist_id')
    }
    main_table, link_table, fk_id = table_map[mode]

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
               l.datetime_added, l.duration, tl.location, tl.filesize
        FROM library l 
        JOIN track_locations tl ON l.location = tl.id
        WHERE l.id IN ({placeholders})
    """
    cues_query = f"SELECT track_id, position FROM cues WHERE track_id IN ({placeholders}) AND type = 1"

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
                track_details[tid]['cues'].append(row['position'])
    except sqlite3.Error as e:
        print(f"Error fetching track metadata: {e}", file=sys.stderr)
        
    return track_details

def build_xml(track_details, collections, is_playlist_mode=False, sort_order=None):
    """Builds the Rekordbox XML structure."""
    dj_playlists = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(dj_playlists, "PRODUCT", Name="rekordbox", Version="6.8.6", Company="AlphaTheta")
    
    collection_node = ET.SubElement(dj_playlists, "COLLECTION", Entries=str(len(track_details)))
    for track_id, data in sorted(track_details.items()):
        location_url = f"file://localhost{quote(data.get('location', ''))}"
        track_attribs = {
            "TrackID": str(track_id), "Name": data.get('title') or "",
            "Artist": data.get('artist') or "", "Album": data.get('album') or "",
            "Grouping": data.get('grouping') or "", "Genre": data.get('genre') or "",
            "Year": str(data.get('year') or ""), "TrackNumber": str(data.get('tracknumber') or ""),
            "Comments": data.get('comment') or "", "Location": location_url,
            "Kind": "MP3 File", "Size": str(data.get('filesize') or "0"),
            "TotalTime": str(round(data.get('duration') or 0.0)),
            "AverageBpm": f"{data.get('bpm', 0.0):.2f}",
            "BitRate": str(data.get('bitrate') or "0"), "SampleRate": str(data.get('samplerate') or "0")
        }
        track_node = ET.SubElement(collection_node, "TRACK", **track_attribs)
        
        samplerate = float(data.get('samplerate', 44100.0) or 44100.0)
        for cue_pos in sorted(data.get('cues', [])):
            pos = (cue_pos / 2.0) / samplerate
            ET.SubElement(track_node, "POSITION_MARK", Name="", Type="0", Start=f"{pos:.3f}", Num="-1")

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
    if is_playlist_mode:
        target_names = args.playlists
        if not target_names:
            target_names = [p.strip() for p in config.get('default_playlists', '').split(',') if p.strip()]
        
        if not target_names:
            print("Error: No playlists specified via CLI or config.")
            sys.exit(1)
            
        collections, track_ids = get_collections(conn, include_names=target_names, mode='playlists')
    else:
        # Default Crate Mode
        collections, track_ids = get_collections(conn, exclude_names=args.exclude_crates, mode='crates')

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

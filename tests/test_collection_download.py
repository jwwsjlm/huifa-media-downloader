from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

from app.core.collection_service import CollectionProbeRequest, CollectionProbeWorker
from app.core.download_options import DownloadOptions
from app.core.download_service import DownloadService, DownloadTask, DownloadWorker
from app.core.external_ytdlp import build_external_ytdlp_command
from app.storage.database import Database
from app.storage.models import MediaItem
from app.ui.download_options import CollectionDetailPage, CollectionEntryModel, CollectionSelectionPage


class DownloadOptionsTests(unittest.TestCase):
    def test_persisted_boolean_strings_are_normalized_explicitly(self) -> None:
        options = DownloadOptions.from_mapping({
            'live_from_start': 'false',
            'wait_for_live': 'true',
            'split_chapters': '0',
            'embed_subtitles': '1',
            'write_thumbnail': 'off',
            'write_info_json': 'yes',
            'organize_task_folder': 'no',
            'prepend_cover_enabled': 'on',
            'write_comments': float('nan'),
        })

        self.assertFalse(options.live_from_start)
        self.assertTrue(options.wait_for_live)
        self.assertFalse(options.split_chapters)
        self.assertTrue(options.embed_subtitles)
        self.assertFalse(options.write_thumbnail)
        self.assertTrue(options.write_info_json)
        self.assertFalse(options.organize_task_folder)
        self.assertTrue(options.prepend_cover_enabled)
        self.assertFalse(options.write_comments)

    def test_non_finite_integer_values_fall_back_without_crashing(self) -> None:
        options = DownloadOptions.from_mapping({
            'first_n': float('inf'),
            'duration_min': float('-inf'),
            'wait_min': float('nan'),
            'prepend_cover_frames': float('inf'),
        })

        self.assertEqual(options.first_n, 0)
        self.assertEqual(options.duration_min, 0)
        self.assertEqual(options.wait_min, 60)
        self.assertEqual(options.prepend_cover_frames, 3)

    def test_collection_worker_cleanup_failure_still_emits_finished(self) -> None:
        request = CollectionProbeRequest(
            request_id='cleanup-finished',
            url='https://example.test/list',
            core_mode='builtin',
        )
        worker = CollectionProbeWorker(request)
        temporary_cookie = self._failing_temporary_cookie()
        worker._temporary_cookie = temporary_cookie
        finished: list[tuple[str, bool, int]] = []
        worker.finished.connect(
            lambda request_id, is_collection, count:
            finished.append((request_id, is_collection, count))
        )

        with patch.object(
            worker,
            '_probe_options',
            side_effect=RuntimeError('probe failed'),
        ):
            worker.run()

        self.assertEqual(temporary_cookie.unlink_count, 1)
        self.assertIsNone(worker._temporary_cookie)
        self.assertEqual(finished, [('cleanup-finished', False, 0)])

    @staticmethod
    def _failing_temporary_cookie():
        class FailingTemporaryCookie:
            def __init__(self) -> None:
                self.unlink_count = 0

            def unlink(self, *, missing_ok: bool = False) -> None:
                self.unlink_count += 1
                raise PermissionError('cookie file is locked')

        return FailingTemporaryCookie()

    def test_manual_content_mode_uses_video_defaults_until_user_confirms(self) -> None:
        options = DownloadOptions.from_mapping({'content_mode': 'manual'})

        self.assertEqual(options.content_mode, 'manual')
        mapped = options.ytdlp_options()
        self.assertIn('format_sort', mapped)
        self.assertNotEqual(mapped.get('format'), 'bestaudio/best')

        worker = DownloadWorker(
            'manual-content',
            'https://example.test/video',
            '.',
            object(),
            quality='best',
            options_json={'content_mode': 'manual'},
        )
        self.assertTrue(worker._manual_selection_required())
        worker.set_format_selector('bestvideo+bestaudio', content_mode='video')
        self.assertFalse(worker._manual_selection_required())

    def test_advanced_options_map_to_both_core_shapes(self) -> None:
        options = DownloadOptions.from_mapping({
            'content_mode': 'audio',
            'audio_format': 'flac',
            'container': 'mkv',
            'collection_order': 'reverse',
            'first_n': 12,
            'playlist_items': '2-8',
            'live_from_start': True,
            'wait_for_live': True,
            'wait_min': 30,
            'wait_max': 90,
            'section_start': '00:01:00',
            'section_end': '00:02:00',
            'split_chapters': True,
            'embed_metadata': True,
            'embed_subtitles': True,
            'embed_thumbnail': True,
            'sponsorblock_mode': 'remove',
            'sponsorblock_categories': ['sponsor', 'intro'],
            'rate_limit': '10M',
        })
        mapped = options.ytdlp_options()
        self.assertEqual(mapped['format'], 'bestaudio/best')
        command = build_external_ytdlp_command('yt-dlp.exe', 'https://example.test/v', mapped, download=True)
        for flag in (
            '--extract-audio', '--audio-format', '--playlist-reverse',
            '--playlist-end', '--playlist-items', '--live-from-start', '--wait-for-video',
            '--download-sections', '--split-chapters', '--embed-metadata', '--embed-subs',
            '--embed-thumbnail', '--sponsorblock-remove', '--limit-rate',
        ):
            self.assertIn(flag, command)
        self.assertNotIn('--merge-output-format', command)
        self.assertNotIn('--remux-video', command)

    def test_video_container_maps_merge_and_progressive_remux_to_both_cores(self) -> None:
        options = DownloadOptions.from_mapping({
            'content_mode': 'video',
            'container': 'mkv',
        })

        mapped = options.ytdlp_options()
        self.assertEqual(mapped['merge_output_format'], 'mkv')
        self.assertIn(
            {'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mkv'},
            mapped['postprocessors'],
        )

        command = build_external_ytdlp_command(
            'yt-dlp.exe', 'https://example.test/v', mapped, download=True,
        )
        self.assertEqual(command[command.index('--merge-output-format') + 1], 'mkv')
        self.assertEqual(command[command.index('--remux-video') + 1], 'mkv')

    def test_audio_track_preferences_keep_fallbacks_and_map_multistreams(self) -> None:
        english = DownloadOptions.from_mapping({'audio_track': 'en'})
        self.assertEqual(english.audio_track, 'en')
        self.assertEqual(
            english.video_format_selector('4k'),
            'bv*[height<=2160]+(ba[language^=en]/ba)/b[height<=2160]',
        )
        self.assertEqual(english.audio_format_selector(), 'ba[language^=en]/ba/b')

        original = DownloadOptions.from_mapping({'audio_track': 'original'})
        self.assertEqual(
            original.video_format_selector('best'),
            'bv*+(ba[format_note*=original]/ba)/b',
        )

        all_tracks = DownloadOptions.from_mapping({'audio_track': 'all'})
        mapped = all_tracks.ytdlp_options()
        self.assertTrue(mapped['allow_multiple_audio_streams'])
        self.assertEqual(
            all_tracks.video_format_selector('best'),
            'bv*+mergeall[vcodec=none]/b',
        )
        command = build_external_ytdlp_command(
            'yt-dlp.exe', 'https://example.test/v', mapped, download=True,
        )
        self.assertIn('--audio-multistreams', command)

    def test_all_audio_tracks_preserve_source_streams_in_audio_mode(self) -> None:
        options = DownloadOptions.from_mapping({
            'content_mode': 'audio',
            'audio_track': 'all',
            'audio_format': 'mp3',
        })
        mapped = options.ytdlp_options()

        self.assertEqual(mapped['format'], 'mergeall[vcodec=none]/ba/b')
        self.assertTrue(mapped['allow_multiple_audio_streams'])
        self.assertFalse(any(
            processor.get('key') == 'FFmpegExtractAudio'
            for processor in mapped.get('postprocessors', ())
        ))

    def test_primary_container_choices_are_bounded_to_auto_mp4_and_mkv(self) -> None:
        self.assertEqual(DownloadOptions.from_mapping({'container': 'mp4'}).container, 'mp4')
        self.assertEqual(DownloadOptions.from_mapping({'container': 'mkv'}).container, 'mkv')
        self.assertEqual(DownloadOptions.from_mapping({'container': 'webm'}).container, 'auto')

    def test_quality_preferences_and_device_target_are_serialized_and_mapped(self) -> None:
        options = DownloadOptions.from_mapping({
            'video_fps': '60',
            'source_video_codec': 'h264',
            'compatibility_target': 'ios',
        })
        self.assertEqual(options.effective_container(), 'mp4')
        self.assertEqual(options.effective_video_codec(), 'h264')
        self.assertEqual(
            options.video_format_selector('2k'),
            'bv*[height<=1440]+ba/b[height<=1440]',
        )
        mapped = options.ytdlp_options()
        self.assertIn('fps:60', mapped['format_sort'])
        self.assertIn('vcodec:h264', mapped['format_sort'])
        self.assertIn('acodec:m4a', mapped['format_sort'])
        self.assertEqual(mapped['merge_output_format'], 'mp4')

    def test_vr_layout_is_a_preference_with_a_normal_format_fallback(self) -> None:
        options = DownloadOptions.from_mapping({
            'vr_mode': '3d180',
            'video_fps': '120',
        })

        selector = options.video_format_selector('4k')
        self.assertIn("format_note~='(?i)(?=.*(?:3d|vr))(?=.*180)'", selector)
        self.assertIn('/bv*[height<=2160]+ba/', selector)
        self.assertIn('fps:120', options.ytdlp_options()['format_sort'])

    def test_collection_filter_marks_completed_and_unavailable(self) -> None:
        request = CollectionProbeRequest(
            request_id='r1', url='https://example.test/list',
            options={'duration_min': 60}, completed_source_keys={'youtube:done'},
        )
        worker = CollectionProbeWorker(request)
        completed = worker._entry({
            'extractor_key': 'Youtube', 'id': 'done', 'url': 'https://example.test/done',
            'title': 'Done', 'duration': 120,
        }, 1, DownloadOptions.from_mapping(request.options))
        self.assertTrue(completed.completed)
        self.assertFalse(completed.selected)
        private = worker._entry({
            'extractor_key': 'Youtube', 'id': 'private', 'url': 'https://example.test/private',
            'availability': 'private', 'duration': 120,
        }, 2, DownloadOptions())
        self.assertFalse(private.downloadable)
        self.assertEqual(private.disabled_reason, 'private')

    def test_collection_filter_uses_normalized_title_when_link_is_not_stable(self) -> None:
        request = CollectionProbeRequest(
            request_id='r-title',
            url='https://example.test/list',
            completed_titles={'  Demo   Video  '},
        )
        worker = CollectionProbeWorker(request)

        entry = worker._entry({
            'extractor_key': 'Generic',
            'id': 'new-url',
            'url': 'https://cdn.example.test/signed?id=2',
            'title': 'demo video',
        }, 1, DownloadOptions())

        self.assertTrue(entry.completed)
        self.assertFalse(entry.selected)

    def test_collection_entry_non_finite_numbers_degrade_to_unknown(self) -> None:
        worker = CollectionProbeWorker(CollectionProbeRequest(
            request_id='invalid-numbers',
            url='https://example.test/list',
        ))

        entry = worker._entry({
            'id': 'invalid-numbers',
            'url': 'https://example.test/video',
            'duration': float('inf'),
            'tbr': float('nan'),
        }, 1, DownloadOptions(duration_min=60))

        self.assertTrue(entry.downloadable)
        self.assertEqual(entry.duration, 0.0)
        self.assertEqual(entry.estimated_bytes, 0)

    def test_collection_entry_huge_filesize_is_bounded_for_sqlite(self) -> None:
        worker = CollectionProbeWorker(CollectionProbeRequest(
            request_id='huge-filesize',
            url='https://example.test/list',
        ))

        entry = worker._entry({
            'id': 'huge-filesize',
            'url': 'https://example.test/video',
            'filesize': 10 ** 100,
        }, 1, DownloadOptions())

        self.assertEqual(entry.estimated_bytes, (1 << 63) - 1)

    def test_nested_collection_does_not_override_missing_or_private_state(self) -> None:
        worker = CollectionProbeWorker(CollectionProbeRequest(
            request_id='nested-unavailable',
            url='https://example.test/list',
        ))
        missing = worker._entry({
            '_type': 'playlist',
            'id': 'missing',
            'title': 'Missing URL',
            'entries': [],
        }, 1, DownloadOptions())
        private = worker._entry({
            '_type': 'playlist',
            'id': 'private',
            'title': 'Private Collection',
            'url': 'https://example.test/private',
            'availability': 'private',
        }, 2, DownloadOptions())

        self.assertEqual(missing.entry_kind, 'collection')
        self.assertFalse(missing.downloadable)
        self.assertFalse(missing.selected)
        self.assertEqual(missing.disabled_reason, 'missing_url')
        self.assertFalse(private.downloadable)
        self.assertFalse(private.selected)
        self.assertEqual(private.disabled_reason, 'private')

    def test_collection_cancel_does_not_run_process_termination_on_ui_caller(self) -> None:
        worker = CollectionProbeWorker(CollectionProbeRequest(
            request_id='cancel-fast',
            url='https://example.test/list',
        ))
        worker._process = object()

        with patch(
            'app.core.collection_service.terminate_external_ytdlp_process',
        ) as terminate:
            worker.cancel()

        self.assertTrue(worker._cancel.is_set())
        terminate.assert_not_called()

    def test_external_collection_cancel_flushes_partial_batch_and_clears_process(self) -> None:
        request = CollectionProbeRequest(
            request_id='external-cancel',
            url='https://example.test/list',
            batch_size=40,
        )
        worker = CollectionProbeWorker(request)
        process = object()
        reader = object()
        lines = object()
        batches: list[list[dict]] = []
        worker.entries.connect(lambda _request_id, entries: batches.append(entries))

        def pump(_process, _reader, _lines, *, cancel_event, consume_line):
            consume_line(json.dumps({
                'id': 'one',
                'title': 'One',
                'url': 'https://example.test/one',
                'playlist_id': 'list',
                'playlist_title': 'List',
            }))
            cancel_event.set()
            raise InterruptedError('cancelled')

        with patch(
            'app.core.collection_service.start_external_ytdlp_process',
            return_value=process,
        ), patch(
            'app.core.collection_service.start_external_ytdlp_output_reader',
            return_value=(lines, reader),
        ), patch(
            'app.core.collection_service.pump_external_ytdlp_output',
            side_effect=pump,
        ), patch(
            'app.core.collection_service.terminate_external_ytdlp_process',
        ) as terminate, patch(
            'app.core.collection_service.finish_external_ytdlp_output_reader',
        ) as finish:
            is_collection, count = worker._run_external(
                'yt-dlp.exe',
                {},
                DownloadOptions(),
            )

        self.assertTrue(is_collection)
        self.assertEqual(count, 1)
        self.assertEqual([[entry['title'] for entry in batch] for batch in batches], [['One']])
        terminate.assert_called_once_with(process)
        finish.assert_called_once_with(process, reader)
        self.assertIsNone(worker._process)

    def test_external_collection_reader_start_failure_clears_process_reference(self) -> None:
        worker = CollectionProbeWorker(CollectionProbeRequest(
            request_id='reader-failure',
            url='https://example.test/list',
        ))
        process = object()

        with patch(
            'app.core.collection_service.start_external_ytdlp_process',
            return_value=process,
        ), patch(
            'app.core.collection_service.start_external_ytdlp_output_reader',
            side_effect=RuntimeError('reader unavailable'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'reader unavailable'):
                worker._run_external('yt-dlp.exe', {}, DownloadOptions())

        self.assertIsNone(worker._process)


class CollectionPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'app.db')
        self.service = DownloadService(self.db)
        self.service._start_next = lambda: None

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_new_task_insert_failure_does_not_publish_ghost_state(self) -> None:
        with (
            patch.object(
                self.db,
                'insert_download_task',
                side_effect=sqlite3.OperationalError('disk full'),
            ),
            patch.object(self.service.logs, 'clear') as clear_log,
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk full'):
                self.service.enqueue(
                    'https://example.test/not-created',
                    self.temp.name,
                    start_immediately=False,
                )

        self.assertFalse(self.service.tasks)
        self.assertFalse(self.service.queue)
        self.assertFalse(self.service._task_index.states)
        self.assertEqual(self.service.task_statistics()['total'], 0)
        self.assertEqual(self.db.list_download_tasks(), [])
        clear_log.assert_not_called()

    def test_registration_failure_rolls_back_the_inserted_task(self) -> None:
        with patch.object(
            self.service,
            '_register_task',
            side_effect=RuntimeError('index failed'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'index failed'):
                self.service.enqueue(
                    'https://example.test/rollback',
                    self.temp.name,
                    start_immediately=False,
                )

        self.assertFalse(self.service.tasks)
        self.assertFalse(self.service.queue)
        self.assertEqual(self.db.list_download_tasks(), [])

    def test_failed_existing_task_update_keeps_cached_indexes_on_durable_state(self) -> None:
        task_id = self.service.enqueue(
            'https://example.test/update-failure',
            self.temp.name,
            start_immediately=False,
        )
        task = self.service.tasks[task_id]
        task.status = 'completed'
        self.service._progress_persistence.pending[task_id] = task

        with patch.object(
            self.db,
            'update_download_task',
            side_effect=sqlite3.OperationalError('database busy'),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'database busy'):
                self.service._persist(task)

        self.assertEqual(self.service._task_index.states[task_id][1], 'queued')
        self.assertEqual(self.service.task_statistics()['queued'], 1)
        self.assertEqual(self.service.task_statistics()['completed'], 0)
        self.assertIs(self.service._progress_persistence.pending[task_id], task)
        row = self.db.list_download_tasks()[0]
        self.assertEqual(str(row['status']), 'queued')

    def test_existing_task_update_never_resurrects_a_deleted_database_row(self) -> None:
        task_id = self.service.enqueue(
            'https://example.test/deleted-before-update',
            self.temp.name,
            start_immediately=False,
        )
        task = self.service.tasks[task_id]
        self.db.delete_download_task(task_id)
        task.status = 'failed'

        with self.assertRaisesRegex(LookupError, task_id):
            self.service._persist(task)

        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertEqual(self.service._task_index.states[task_id][1], 'queued')

    def test_collection_probe_failure_is_persisted_by_the_service_boundary(self) -> None:
        task_id = self.service.create_collection(
            'https://example.test/collection',
            self.temp.name,
        )
        updated: list[DownloadTask] = []
        self.service.task_updated.connect(updated.append)

        self.assertTrue(
            self.service.fail_collection_probe(task_id, 'probe unavailable')
        )

        task = self.service.tasks[task_id]
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.stage, 'failed')
        self.assertEqual(task.error, 'probe unavailable')
        self.assertIs(updated[-1], task)
        row = self.db.list_download_tasks()[0]
        self.assertEqual(str(row['status']), 'failed')
        self.assertEqual(str(row['error']), 'probe unavailable')

    def test_post_commit_logging_and_scheduler_failures_keep_one_queued_task(self) -> None:
        added: list[DownloadTask] = []
        self.service.task_added.connect(added.append)
        with (
            patch.object(
                self.service.logs,
                'clear',
                side_effect=PermissionError('log is locked'),
            ),
            patch.object(
                self.service,
                '_start_next',
                side_effect=RuntimeError('scheduler failed'),
            ),
        ):
            task_id = self.service.enqueue(
                'https://example.test/queued-once',
                self.temp.name,
            )

        self.assertEqual([task.id for task in added], [task_id])
        self.assertEqual(list(self.service.queue), [task_id])
        self.assertEqual(set(self.service.tasks), {task_id})
        rows = self.db.list_download_tasks()
        self.assertEqual([str(row['id']) for row in rows], [task_id])
        self.assertEqual(str(rows[0]['status']), 'queued')

    def test_new_task_id_collision_never_overwrites_existing_record(self) -> None:
        first_id = self.service.enqueue(
            'https://example.test/original',
            self.temp.name,
            start_immediately=False,
        )

        with patch.object(self.service, '_new_task_id', return_value=first_id):
            with self.assertRaises(sqlite3.IntegrityError):
                self.service.enqueue(
                    'https://example.test/collision',
                    self.temp.name,
                    start_immediately=False,
                )

        self.assertEqual(set(self.service.tasks), {first_id})
        rows = self.db.list_download_tasks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]['url']), 'https://example.test/original')

    def test_collection_parent_is_inserted_once_in_its_final_initial_state(self) -> None:
        added: list[DownloadTask] = []
        self.service.task_added.connect(added.append)
        with (
            patch.object(
                self.db,
                'insert_download_task',
                wraps=self.db.insert_download_task,
            ) as insert_task,
            patch.object(
                self.db,
                'upsert_download_task',
                wraps=self.db.upsert_download_task,
            ) as update_task,
        ):
            parent_id = self.service.create_collection(
                'https://example.test/one-write-list',
                self.temp.name,
                title='One-write list',
            )

        self.assertEqual(insert_task.call_count, 1)
        update_task.assert_not_called()
        self.assertEqual([task.id for task in added], [parent_id])
        parent = self.service.tasks[parent_id]
        self.assertEqual(parent.status, 'parsing_collection')
        self.assertEqual(parent.root_task_id, parent_id)
        self.assertFalse(self.service.queue)
        row = self.db.list_download_tasks()[0]
        self.assertEqual(str(row['status']), 'parsing_collection')
        self.assertEqual(str(row['root_task_id']), parent_id)

    def test_collection_materialization_database_id_collision_is_atomic(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/database-collision-list',
            self.temp.name,
            title='Database collision list',
        )
        hidden = DownloadTask(
            'database-only-id',
            'https://example.test/existing-database-row',
            self.temp.name,
        )
        self.db.insert_download_task(hidden)

        with patch.object(
            self.service,
            '_new_task_id',
            return_value=hidden.id,
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.service.enqueue_collection_entries(parent_id, [{
                    'url': 'https://example.test/new-child',
                    'source_key': 'generic:new-child',
                    'title': 'New child',
                    'index': 1,
                }])

        self.assertEqual(self.service.collection_children(parent_id), [])
        parent = self.service.tasks[parent_id]
        self.assertEqual(parent.status, 'parsing_collection')
        rows = {str(row['id']): row for row in self.db.list_download_tasks()}
        self.assertEqual(set(rows), {parent_id, hidden.id})
        self.assertEqual(str(rows[parent_id]['status']), 'parsing_collection')
        self.assertEqual(str(rows[hidden.id]['url']), hidden.url)

    def test_parent_children_restore_and_aggregate(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list', self.temp.name,
            title='List', source_key='generic:list', options_json={
                'collection_mode': 'select',
                'content_mode': 'video',
                'container': 'mkv',
            },
        )
        child_ids = self.service.enqueue_collection_entries(parent_id, [
            {'url': 'https://example.test/1', 'source_key': 'generic:1', 'title': 'One', 'index': 1, 'entry_kind': 'video'},
            {'url': 'https://example.test/2', 'source_key': 'generic:2', 'title': 'Two', 'index': 2, 'entry_kind': 'video'},
        ])
        self.assertEqual(len(child_ids), 2)
        first = self.service.tasks[child_ids[0]]
        self.assertEqual(first.options_json['content_mode'], 'video')
        self.assertEqual(first.options_json['container'], 'mkv')
        first_media = Path(self.temp.name) / 'one.mp4'
        first_media.write_bytes(b'video')
        first.status = 'completed'
        first.progress = 100
        first.media_path = str(first_media)
        second = self.service.tasks[child_ids[1]]
        second.status = 'failed'
        self.service._persist(first)
        self.service._persist(second)
        self.service._refresh_collection(parent_id)
        parent = self.service.tasks[parent_id]
        self.assertEqual(parent.status, 'partial_failed')
        self.assertEqual(parent.options_json['_collection']['completed'], 1)
        self.assertEqual(parent.options_json['_collection']['failed'], 1)

        restored = DownloadService(self.db)
        restored._start_next = lambda: None
        restored.restore_tasks()
        self.assertEqual(len(restored.collection_children(parent_id)), 2)
        self.assertEqual(restored.tasks[parent_id].status, 'partial_failed')
        self.assertEqual(restored.tasks[parent_id].options_json['_collection']['failed'], 1)

    def test_collection_summary_persist_failure_restores_parent_and_schedules_retry(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/retry-summary',
            self.temp.name,
            title='Retry summary',
        )
        child_id = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/retry-summary/1',
            'source_key': 'generic:retry-summary-1',
            'title': 'One',
            'index': 1,
            'entry_kind': 'video',
        }])[0]
        parent = self.service.tasks[parent_id]
        original_status = parent.status
        original_stage = parent.stage
        original_progress = parent.progress
        original_options = json.loads(json.dumps(parent.options_json))
        child = self.service.tasks[child_id]
        child.status = 'completed'
        child.progress = 100.0
        self.service._persist(child)

        with patch.object(
            self.service,
            '_persist_progress',
            side_effect=sqlite3.OperationalError('database busy'),
        ):
            self.service._refresh_collection(parent_id)

        self.assertEqual(parent.status, original_status)
        self.assertEqual(parent.stage, original_stage)
        self.assertEqual(parent.progress, original_progress)
        self.assertEqual(parent.options_json, original_options)
        self.assertIn(parent_id, self.service._pending_collection_refreshes)
        self.assertTrue(self.service._collection_refresh_timer.isActive())
        self.service._collection_refresh_timer.stop()

    def test_collection_materialization_commits_parent_and_children_once(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/atomic-list',
            self.temp.name,
            title='Atomic list',
        )

        with patch.object(
            self.db,
            'materialize_download_tasks',
            wraps=self.db.materialize_download_tasks,
        ) as persist_materialization:
            child_ids = self.service.enqueue_collection_entries(parent_id, [{
                'url': 'https://example.test/atomic/1',
                'source_key': 'generic:atomic-1',
                'title': 'Atomic one',
                'index': 1,
            }])

        self.assertEqual(len(child_ids), 1)
        self.assertEqual(persist_materialization.call_count, 1)
        persisted_parent, persisted_children = persist_materialization.call_args.args
        self.assertEqual(persisted_parent.id, parent_id)
        self.assertEqual([task.id for task in persisted_children], [child_ids[0]])
        self.assertIsNot(persisted_parent, self.service.tasks[parent_id])

    def test_collection_materialization_failure_does_not_publish_partial_state(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/failed-atomic-list',
            self.temp.name,
            title='Failed atomic list',
        )
        parent = self.service.tasks[parent_id]
        original_state = (
            parent.status,
            parent.stage,
            parent.stage_text,
            json.dumps(parent.options_json, ensure_ascii=False, sort_keys=True),
        )
        original_queue = tuple(self.service.queue)
        original_task_ids = set(self.service.tasks)
        original_persist_times = dict(self.service._progress_persistence.persisted_at)
        original_task_indexes = dict(self.service._task_index.states)
        added_batches: list[list[DownloadTask]] = []
        updated_tasks: list[DownloadTask] = []
        self.service.tasks_added.connect(lambda batch: added_batches.append(list(batch)))
        self.service.task_updated.connect(updated_tasks.append)

        with patch.object(
            self.db,
            'materialize_download_tasks',
            side_effect=sqlite3.OperationalError('database busy'),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'database busy'):
                self.service.enqueue_collection_entries(parent_id, [{
                    'url': 'https://example.test/failed-atomic/1',
                    'source_key': 'generic:failed-atomic-1',
                    'title': 'Must not appear',
                    'index': 1,
                }])

        self.assertEqual(self.service.collection_children(parent_id), [])
        self.assertEqual(tuple(self.service.queue), original_queue)
        self.assertEqual(set(self.service.tasks), original_task_ids)
        self.assertEqual(
            self.service._progress_persistence.persisted_at,
            original_persist_times,
        )
        self.assertEqual(self.service._task_index.states, original_task_indexes)
        self.assertEqual(added_batches, [])
        self.assertEqual(updated_tasks, [])
        self.assertEqual(
            (
                parent.status,
                parent.stage,
                parent.stage_text,
                json.dumps(parent.options_json, ensure_ascii=False, sort_keys=True),
            ),
            original_state,
        )
        rows = self.db.list_download_tasks()
        self.assertEqual([row['id'] for row in rows], [parent_id])

    def test_collection_materialization_retries_task_id_collision_without_overwriting(self) -> None:
        existing_id = self.service.enqueue(
            'https://example.test/existing-task',
            self.temp.name,
            source_key='generic:existing-task',
            start_immediately=False,
        )
        existing_task = self.service.tasks[existing_id]
        parent_id = self.service.create_collection(
            'https://example.test/collision-list',
            self.temp.name,
            title='Collision list',
        )
        replacement_id = 'f' * 16

        with patch(
            'app.core.download_service.uuid4',
            side_effect=(
                SimpleNamespace(hex=existing_id),
                SimpleNamespace(hex=replacement_id),
            ),
        ):
            created = self.service.enqueue_collection_entries(parent_id, [{
                'url': 'https://example.test/collision-list/new',
                'source_key': 'generic:collision-list-new',
                'title': 'New child',
                'index': 1,
            }])

        self.assertEqual(created, [replacement_id])
        self.assertIs(self.service.tasks[existing_id], existing_task)
        self.assertEqual(self.service.tasks[replacement_id].parent_task_id, parent_id)
        self.assertEqual(
            {row['id'] for row in self.db.list_download_tasks()},
            {existing_id, parent_id, replacement_id},
        )

    def test_collection_materialization_with_no_valid_entries_persists_canceled_parent(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/empty-list',
            self.temp.name,
            title='Empty list',
        )
        added_batches: list[list[DownloadTask]] = []
        updated_tasks: list[DownloadTask] = []
        self.service.tasks_added.connect(lambda batch: added_batches.append(list(batch)))
        self.service.task_updated.connect(updated_tasks.append)

        created = self.service.enqueue_collection_entries(parent_id, [
            {'entry_kind': 'collection', 'url': 'https://example.test/nested'},
            {'entry_kind': 'video', 'url': ''},
        ])

        parent = self.service.tasks[parent_id]
        row = next(row for row in self.db.list_download_tasks() if row['id'] == parent_id)
        self.assertEqual(created, [])
        self.assertEqual(parent.status, 'canceled')
        self.assertEqual(parent.stage, 'canceled')
        self.assertEqual(parent.options_json['_collection']['selected'], 0)
        self.assertEqual(row['status'], 'canceled')
        self.assertEqual(added_batches, [])
        self.assertEqual(updated_tasks, [parent])

    def test_download_task_batch_failure_rolls_back_and_releases_transaction(self) -> None:
        valid = DownloadTask(
            'valid-before-failure',
            'https://example.test/valid-before-failure',
            self.temp.name,
        )
        invalid = DownloadTask(
            'invalid-batch-row',
            None,  # type: ignore[arg-type]
            self.temp.name,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.upsert_download_tasks([valid, invalid])

        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertFalse(self.db.conn.in_transaction)

        recovery = DownloadTask(
            'valid-after-failure',
            'https://example.test/valid-after-failure',
            self.temp.name,
        )
        self.db.upsert_download_task(recovery)
        self.assertEqual(
            [row['id'] for row in self.db.list_download_tasks()],
            [recovery.id],
        )

    def test_collection_probe_batch_failure_rolls_back_and_releases_transaction(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/probe-transaction',
            self.temp.name,
            title='Probe transaction',
        )
        with self.db._lock:
            self.db.conn.execute(
                """CREATE TRIGGER fail_second_probe_entry
                BEFORE INSERT ON collection_probe_entries
                WHEN NEW.collection_index=2
                BEGIN
                    SELECT RAISE(ABORT, 'injected probe failure');
                END"""
            )
            self.db.conn.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected probe failure'):
            self.db.upsert_collection_probe_entries(parent_id, [
                {'index': 1, 'url': 'https://example.test/probe/1'},
                {'index': 2, 'url': 'https://example.test/probe/2'},
            ])

        self.assertEqual(self.db.list_collection_probe_entries(parent_id), [])
        self.assertFalse(self.db.conn.in_transaction)

        inserted = self.db.upsert_collection_probe_entries(parent_id, [{
            'index': 3,
            'url': 'https://example.test/probe/3',
        }])
        self.assertEqual(inserted, 1)
        self.assertEqual(
            [entry['index'] for entry in self.db.list_collection_probe_entries(parent_id)],
            [3],
        )

    def test_collection_children_do_not_open_one_manual_content_dialog_per_item(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/manual-list',
            self.temp.name,
            quality='custom',
            options_json={
                'collection_mode': 'select',
                'content_mode': 'manual',
            },
        )

        child_ids = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/manual-child',
            'source_key': 'generic:manual-child',
            'title': 'Manual child',
            'index': 1,
            'entry_kind': 'video',
        }])

        child = self.service.tasks[child_ids[0]]
        self.assertEqual(child.quality, 'best')
        self.assertEqual(child.options_json['content_mode'], 'video')
        self.assertNotIn('_collection', child.options_json)
        self.assertNotIn('_collection_materialization', child.options_json)

    def test_restore_defers_old_terminal_children_in_event_loop_batches(self) -> None:
        parent = DownloadTask(
            'restore-large-parent',
            'https://example.test/restore-large',
            self.temp.name,
            task_kind='collection',
            status='completed',
        )
        children = [
            DownloadTask(
                f'restore-child-{index}',
                f'https://example.test/restore/{index}',
                self.temp.name,
                parent_task_id=parent.id,
                root_task_id=parent.id,
                collection_index=index,
                status='completed',
                progress=100,
            )
            for index in range(1, 451)
        ]
        self.db.upsert_download_tasks([parent, *children])

        restored = DownloadService(self.db)
        restored._start_next = lambda: None
        batches: list[int] = []
        restored.tasks_added.connect(lambda batch: batches.append(len(batch)))
        immediate = restored.restore_tasks()

        self.assertEqual(len(immediate), 201)
        self.assertEqual(len(restored.collection_children(parent.id)), 200)
        deadline = time.monotonic() + 2.0
        while len(restored.collection_children(parent.id)) < 450 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)

        self.assertEqual(len(restored.collection_children(parent.id)), 450)
        self.assertEqual(batches, [200, 50])
        restored.shutdown(timeout_ms=0)

    def test_collection_progress_is_weighted_by_bytes(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list-weighted', self.temp.name,
            title='Weighted', source_key='generic:weighted',
        )
        child_ids = self.service.enqueue_collection_entries(parent_id, [
            {'url': 'https://example.test/small', 'source_key': 'generic:small', 'title': 'Small', 'index': 1},
            {'url': 'https://example.test/large', 'source_key': 'generic:large', 'title': 'Large', 'index': 2},
        ])
        small = self.service.tasks[child_ids[0]]
        large = self.service.tasks[child_ids[1]]
        small.status = 'completed'
        small.progress = 100
        small.total_bytes = 10 * 1024 * 1024
        small.downloaded_bytes = small.total_bytes
        large.status = 'downloading'
        large.progress = 0
        large.total_bytes = 100 * 1024 * 1024
        large.downloaded_bytes = 0
        self.service._sync_task_indexes(small)
        self.service._sync_task_indexes(large)

        with patch.object(
            self.service,
            'collection_children',
            side_effect=AssertionError('parent refresh must use incremental aggregates'),
        ):
            self.service._refresh_collection(parent_id)

        parent = self.service.tasks[parent_id]
        self.assertAlmostEqual(parent.progress, 100 / 11, places=2)

    def test_collection_with_unknown_child_does_not_publish_false_total_or_eta(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list-unknown-size', self.temp.name,
            title='Unknown size', source_key='generic:unknown-size',
        )
        child_ids = self.service.enqueue_collection_entries(parent_id, [
            {'url': 'https://example.test/known', 'source_key': 'generic:known', 'title': 'Known', 'index': 1},
            {'url': 'https://example.test/unknown', 'source_key': 'generic:unknown', 'title': 'Unknown', 'index': 2},
        ])
        known = self.service.tasks[child_ids[0]]
        unknown = self.service.tasks[child_ids[1]]
        known.status = 'downloading'
        known.total_bytes = 100
        known.downloaded_bytes = 50
        known.speed_bps = 10
        unknown.status = 'downloading'
        unknown.total_bytes = 0
        unknown.downloaded_bytes = 500
        unknown.progress = 25
        unknown.speed_bps = 20
        self.service._sync_task_indexes(known)
        self.service._sync_task_indexes(unknown)

        self.service._refresh_collection(parent_id)

        parent = self.service.tasks[parent_id]
        self.assertEqual(parent.status, 'downloading')
        self.assertEqual(parent.downloaded_bytes, 550)
        self.assertEqual(parent.total_bytes, 0)
        self.assertEqual(parent.eta, '')
        self.assertEqual(parent.speed_bps, 30)

    def test_collection_known_byte_display_is_clamped_to_total(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list-overrun', self.temp.name,
            title='Overrun', source_key='generic:overrun',
        )
        child_ids = self.service.enqueue_collection_entries(parent_id, [
            {'url': 'https://example.test/overrun', 'source_key': 'generic:overrun-child', 'title': 'Overrun child', 'index': 1},
        ])
        child = self.service.tasks[child_ids[0]]
        child.status = 'downloading'
        child.total_bytes = 100
        child.downloaded_bytes = 180
        child.speed_bps = 10
        self.service._sync_task_indexes(child)

        self.service._refresh_collection(parent_id)

        parent = self.service.tasks[parent_id]
        self.assertEqual(parent.downloaded_bytes, 100)
        self.assertEqual(parent.total_bytes, 100)
        self.assertEqual(parent.progress, 100.0)
        self.assertEqual(parent.eta, '')

    def test_collection_child_contribution_tolerates_corrupted_live_numbers(self) -> None:
        task = DownloadTask(
            'corrupted-child',
            'https://example.test/corrupted-child',
            self.temp.name,
            parent_task_id='parent',
            status='downloading',
        )
        task.total_bytes = 'invalid'
        task.downloaded_bytes = float('inf')
        task.progress = 'bad'
        task.speed_bps = float('nan')

        contribution = self.service._collection_child_contribution(task)

        self.assertIsNotNone(contribution)
        assert contribution is not None
        self.assertEqual(contribution.known_size, 0)
        self.assertEqual(contribution.downloaded_bytes, 0)
        self.assertEqual(contribution.unknown_fraction, 0.0)
        self.assertEqual(contribution.speed_bps, 0.0)

    def test_collection_aggregate_treats_conversion_as_active_and_propagates_partial_failure(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/aggregate-status', self.temp.name, title='Aggregate status'
        )
        child_ids = self.service.enqueue_collection_entries(parent_id, [
            {'url': 'https://example.test/processing', 'source_key': 'generic:processing', 'title': 'Processing', 'index': 1},
            {'url': 'https://example.test/nested', 'source_key': 'generic:nested', 'title': 'Nested', 'index': 2},
        ])
        processing = self.service.tasks[child_ids[0]]
        nested = self.service.tasks[child_ids[1]]
        processing.status = 'processing'
        nested.status = 'partial_failed'
        self.service._sync_task_indexes(processing)
        self.service._sync_task_indexes(nested)

        self.service._refresh_collection(parent_id)
        self.assertEqual(self.service.tasks[parent_id].status, 'downloading')

        processing.status = 'completed'
        processing.progress = 100
        self.service._sync_task_indexes(processing)
        self.service._refresh_collection(parent_id)
        self.assertEqual(self.service.tasks[parent_id].status, 'partial_failed')

    def test_completed_media_identities_include_tasks_and_retained_media(self) -> None:
        task_id = self.service.enqueue(
            'https://example.test/1', self.temp.name,
            source_key='generic:1', start_immediately=False,
        )
        task = self.service.tasks[task_id]
        task.status = 'completed'
        task.title = 'Demo Video'
        self.service._persist(task)
        self.db.add_media(MediaItem(
            source_url='https://example.test/archived',
            title='Archived Video',
        ))

        source_keys, urls, titles = self.db.completed_media_identities()

        self.assertEqual(source_keys, {'generic:1'})
        self.assertEqual(
            urls,
            {'https://example.test/1', 'https://example.test/archived'},
        )
        self.assertEqual(titles, {'Demo Video', 'Archived Video'})

    def test_parsing_collection_blocks_auto_mode_duplicate_submission(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list',
            self.temp.name,
            quality='best',
            playlist_mode='playlist',
        )

        duplicate = self.service.find_active_duplicate(
            'https://example.test/list',
            self.temp.name,
            'best',
            'auto',
        )

        self.assertEqual(duplicate, parent_id)

    def test_single_result_reuses_collection_placeholder_task_id(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/video',
            self.temp.name,
            title='Parsing',
            playlist_mode='playlist',
        )

        resolved = self.service.resolve_collection_as_video(
            parent_id,
            title='Single video',
            source_key='generic:video',
        )

        self.assertTrue(resolved)
        task = self.service.tasks[parent_id]
        self.assertEqual(task.task_kind, 'video')
        self.assertEqual(task.playlist_mode, 'single')
        self.assertEqual(task.title, 'Single video')
        self.assertEqual(task.status, 'queued')
        self.assertIn(parent_id, self.service.queue)
        row = next(row for row in self.db.list_download_tasks() if row['id'] == parent_id)
        self.assertEqual(row['task_kind'], 'video')
        self.assertEqual(row['playlist_mode'], 'single')

    def test_single_result_discards_alias_placeholder_for_active_video(self) -> None:
        active_id = self.service.enqueue(
            'https://example.test/watch/original',
            self.temp.name,
            source_key='youtube:CaseSensitiveId',
            start_immediately=False,
        )
        self.service.tasks[active_id].title = 'Resolved video title'
        placeholder_id = self.service.create_collection(
            'https://short.example/alias',
            self.temp.name,
            title='Parsing alias',
        )

        resolved = self.service.resolve_collection_as_video(
            placeholder_id,
            title='Resolved video title',
            source_key='YouTube:CaseSensitiveId',
        )

        self.assertTrue(resolved)
        self.assertIn(active_id, self.service.tasks)
        self.assertNotIn(placeholder_id, self.service.tasks)
        self.assertNotIn(
            placeholder_id,
            {str(row['id']) for row in self.db.list_download_tasks()},
        )

    def test_collection_actions_cascade_to_children(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list', self.temp.name, title='List'
        )
        children = self.service.enqueue_collection_entries(parent_id, [
            {'url': 'https://example.test/1', 'source_key': 'generic:1', 'title': 'One', 'index': 1, 'entry_kind': 'video'},
            {'url': 'https://example.test/2', 'source_key': 'generic:2', 'title': 'Two', 'index': 2, 'entry_kind': 'video'},
        ])
        self.service.pause(parent_id)
        self.assertEqual({self.service.tasks[item].status for item in children}, {'paused'})
        self.assertEqual(self.service.tasks[parent_id].status, 'paused')
        self.service.resume(parent_id)
        self.assertEqual({self.service.tasks[item].status for item in children}, {'queued'})
        self.service.cancel(parent_id)
        self.assertEqual({self.service.tasks[item].status for item in children}, {'canceled'})
        self.service.retry(parent_id)
        self.assertEqual({self.service.tasks[item].status for item in children}, {'queued'})

    def test_collection_pause_is_one_atomic_child_transition(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/atomic-pause', self.temp.name, title='Atomic pause'
        )
        child_ids = self.service.enqueue_collection_entries(parent_id, [
            {'url': 'https://example.test/atomic-pause/1', 'source_key': 'generic:atomic-pause-1', 'title': 'One', 'index': 1},
            {'url': 'https://example.test/atomic-pause/2', 'source_key': 'generic:atomic-pause-2', 'title': 'Two', 'index': 2},
        ])
        original_queue = tuple(self.service.queue)
        updated: list[str] = []
        self.service.task_updated.connect(lambda task: updated.append(task.id))

        with patch.object(
            self.db,
            'update_download_tasks',
            side_effect=sqlite3.OperationalError('database busy'),
        ) as batch_update:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'database busy'):
                self.service.pause(parent_id)

        batch_update.assert_called_once()
        self.assertEqual(
            {self.service.tasks[child_id].status for child_id in child_ids},
            {'queued'},
        )
        self.assertEqual(tuple(self.service.queue), original_queue)
        self.assertEqual(updated, [])
        rows = {
            str(row['id']): str(row['status'])
            for row in self.db.list_download_tasks()
        }
        self.assertEqual({rows[child_id] for child_id in child_ids}, {'queued'})

    def test_collection_cancel_persist_failure_does_not_cancel_workers(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/atomic-cancel', self.temp.name, title='Atomic cancel'
        )
        child_id = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/atomic-cancel/1',
            'source_key': 'generic:atomic-cancel-1',
            'title': 'One',
            'index': 1,
        }])[0]
        child = self.service.tasks[child_id]
        child.status = 'downloading'
        child.stage = 'downloading'
        self.service._persist(child)
        cancel_reasons: list[str] = []
        self.service.workers[child_id] = SimpleNamespace(
            cancel=cancel_reasons.append,
        )
        self.service._pending_runtime_retries.add(child_id)

        with patch.object(
            self.db,
            'update_download_tasks',
            side_effect=sqlite3.OperationalError('disk full'),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk full'):
                self.service.cancel(parent_id)

        self.assertEqual(child.status, 'downloading')
        self.assertFalse(child.cancel_requested)
        self.assertEqual(cancel_reasons, [])
        self.assertIn(child_id, self.service._pending_runtime_retries)
        row = next(
            row for row in self.db.list_download_tasks()
            if row['id'] == child_id
        )
        self.assertEqual(row['status'], 'downloading')

    def test_collection_cancel_clears_retry_waiting_for_runtime_cleanup(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/cancel-pending-retry',
            self.temp.name,
            title='Cancel pending retry',
        )
        child_id = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/cancel-pending-retry/1',
            'source_key': 'generic:cancel-pending-retry-1',
            'title': 'One',
            'index': 1,
        }])[0]
        child = self.service.tasks[child_id]
        child.status = 'failed'
        child.error = 'network failure'
        child.stage = 'failed'
        self.service._persist(child)
        self.service._pending_runtime_retries.add(child_id)

        self.service.cancel(parent_id)

        self.assertNotIn(child_id, self.service._pending_runtime_retries)
        self.assertEqual(child.status, 'failed')

    def test_canceling_paused_collection_child_clears_control_flags(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/cancel-paused-flags',
            self.temp.name,
            title='Cancel paused flags',
        )
        child_id = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/cancel-paused-flags/1',
            'source_key': 'generic:cancel-paused-flags-1',
            'title': 'One',
            'index': 1,
        }])[0]

        self.service.pause(parent_id)
        child = self.service.tasks[child_id]
        child.pause_requested = True
        self.service.cancel(parent_id)

        self.assertEqual(child.status, 'canceled')
        self.assertFalse(child.pause_requested)
        self.assertFalse(child.cancel_requested)

    def test_collection_summary_tracks_manual_conversion_cancellation(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/conversion-parent',
            self.temp.name,
            title='Conversion parent',
        )
        child_id = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/conversion-parent/1',
            'source_key': 'generic:conversion-parent-1',
            'title': 'One',
            'index': 1,
        }])[0]
        child = self.service.tasks[child_id]
        child.status = 'completed'
        child.progress = 100.0
        self.service._persist(child)
        self.service._refresh_collection(parent_id)
        self.assertEqual(self.service.tasks[parent_id].status, 'completed')

        child.status = 'processing'
        child.stage = 'transcoding'
        self.service._sync_task_indexes(child)
        self.service._mark_conversion_canceling(child, 'canceling conversion')
        self.assertEqual(self.service.tasks[parent_id].status, 'downloading')

        self.service._on_completed_conversion_canceled(child_id)
        self.assertEqual(child.status, 'completed')
        self.assertEqual(self.service.tasks[parent_id].status, 'completed')

    def test_collection_actions_cover_nested_collection_leaves(self) -> None:
        root_id = self.service.create_collection(
            'https://example.test/root-list', self.temp.name, title='Root list'
        )
        nested = DownloadTask(
            'nested-collection-action',
            'https://example.test/nested-list',
            self.temp.name,
            task_kind='collection',
            parent_task_id=root_id,
            root_task_id=root_id,
            collection_index=1,
            title='Nested list',
            status='queued',
        )
        self.db.insert_download_task(nested)
        self.service._register_task(nested)
        leaf_id = self.service.enqueue_collection_entries(nested.id, [{
            'url': 'https://example.test/nested-list/1',
            'source_key': 'generic:nested-list-1',
            'title': 'Nested leaf',
            'index': 1,
        }])[0]

        self.service.pause(root_id)
        self.assertEqual(self.service.tasks[leaf_id].status, 'paused')
        self.assertEqual(self.service.tasks[nested.id].status, 'paused')
        self.assertEqual(self.service.tasks[root_id].status, 'paused')

        self.service.resume(root_id)
        self.assertEqual(self.service.tasks[leaf_id].status, 'queued')
        self.assertEqual(self.service.tasks[nested.id].status, 'queued')
        self.assertEqual(self.service.tasks[root_id].status, 'queued')

    def test_idle_collection_delete_is_atomic_across_database_memory_and_files(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/delete-atomic-tree',
            self.temp.name,
            title='Delete atomic tree',
        )
        child_id = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/delete-atomic-tree/1',
            'source_key': 'generic:delete-atomic-tree-1',
            'title': 'Delete child',
            'index': 1,
        }])[0]
        partial = Path(self.temp.name) / 'Delete child [delete-child].f137.mp4.part'
        partial.write_bytes(b'partial')
        child = self.service.tasks[child_id]
        child.status = 'paused'
        child.current_filename = str(partial.with_suffix(''))
        self.service._persist(child)
        deleted_signals: list[str] = []
        self.service.task_deleted.connect(deleted_signals.append)

        with patch.object(
            self.db,
            'delete_download_task_tree',
            side_effect=RuntimeError('database busy'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'database busy'):
                self.service.delete_task(parent_id, delete_files=True)

        self.assertIn(parent_id, self.service.tasks)
        self.assertIn(child_id, self.service.tasks)
        self.assertTrue(partial.is_file())
        self.assertEqual(
            {row['id'] for row in self.db.list_download_tasks()},
            {parent_id, child_id},
        )
        self.assertEqual(deleted_signals, [])

        self.assertTrue(self.service.delete_task(parent_id, delete_files=True))
        self.assertNotIn(parent_id, self.service.tasks)
        self.assertNotIn(child_id, self.service.tasks)
        self.assertFalse(partial.exists())
        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertEqual(set(deleted_signals), {parent_id, child_id})

    def test_running_collection_waits_for_download_and_conversion_before_atomic_delete(self) -> None:
        class CancelRecorder:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def cancel(self, *args) -> None:
                self.calls.append(args)

        parent_id = self.service.create_collection(
            'https://example.test/delete-running-tree',
            self.temp.name,
            title='Delete running tree',
        )
        child_ids = self.service.enqueue_collection_entries(parent_id, [
            {
                'url': 'https://example.test/delete-running-tree/download',
                'source_key': 'generic:delete-running-download',
                'title': 'Running download',
                'index': 1,
            },
            {
                'url': 'https://example.test/delete-running-tree/conversion',
                'source_key': 'generic:delete-running-conversion',
                'title': 'Running conversion',
                'index': 2,
            },
        ])
        download_id, conversion_id = child_ids
        download_file = Path(self.temp.name) / 'Running download [download].mp4.part'
        conversion_file = Path(self.temp.name) / 'Running conversion [conversion].mp4.part'
        download_file.write_bytes(b'download')
        conversion_file.write_bytes(b'conversion')
        self.service.tasks[download_id].current_filename = str(download_file.with_suffix(''))
        self.service.tasks[conversion_id].current_filename = str(conversion_file.with_suffix(''))
        download_worker = CancelRecorder()
        conversion_worker = CancelRecorder()
        self.service.workers[download_id] = download_worker
        self.service.conversion_workers[conversion_id] = conversion_worker
        deleted_signals: list[str] = []
        self.service.task_deleted.connect(deleted_signals.append)
        durable_statuses_before = {
            str(row['id']): str(row['status'])
            for row in self.db.list_download_tasks()
        }

        self.assertTrue(self.service.delete_task(parent_id, delete_files=False))
        self.assertTrue(self.service.delete_task(parent_id, delete_files=True))

        self.assertEqual(download_worker.calls, [('delete',)])
        self.assertEqual(conversion_worker.calls, [()])
        self.assertEqual(
            {self.service.tasks[task_id].status for task_id in (parent_id, *child_ids)},
            {'canceling'},
        )
        self.assertNotIn(download_id, self.service.queue)
        self.assertNotIn(conversion_id, self.service.queue)
        self.assertEqual(len(self.db.list_download_tasks()), 3)
        self.assertEqual(
            {
                str(row['id']): str(row['status'])
                for row in self.db.list_download_tasks()
            },
            durable_statuses_before,
        )

        self.service._conversion_thread_finished(conversion_id)
        self.assertEqual(len(self.db.list_download_tasks()), 3)
        self.assertIn(parent_id, self.service.tasks)

        with patch.object(
            self.db,
            'delete_download_task_tree',
            side_effect=RuntimeError('database busy'),
        ):
            self.service._thread_finished(download_id)

        self.assertIn(parent_id, self.service.tasks)
        self.assertIn(download_id, self.service.tasks)
        self.assertIn(conversion_id, self.service.tasks)
        self.assertTrue(download_file.is_file())
        self.assertTrue(conversion_file.is_file())
        self.assertEqual(len(self.db.list_download_tasks()), 3)
        self.assertEqual(deleted_signals, [])

        self.assertTrue(self.service.delete_task(parent_id, delete_files=True))
        self.assertEqual(self.service.tasks, {})
        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertFalse(download_file.exists())
        self.assertFalse(conversion_file.exists())
        self.assertEqual(set(deleted_signals), {parent_id, download_id, conversion_id})

    def test_collection_probe_cache_is_paged_filterable_and_durable(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/large-list', self.temp.name, title='Large list'
        )
        entries = [
            {
                'index': index,
                'source_key': f'generic:{index}',
                'url': f'https://example.test/{index}',
                'title': f'Video {index:04d}',
                'uploader': 'Alice' if index % 2 else 'Bob',
                'duration': index,
                'upload_date': f'2026{1 + index % 8:02d}01',
                'downloadable': True,
                'selected': index % 3 != 0,
                'estimated_bytes': index * 1000,
            }
            for index in range(1, 1001)
        ]
        self.assertEqual(self.db.upsert_collection_probe_entries(parent_id, entries), 1000)
        self.assertEqual(self.db.collection_probe_entry_count(parent_id), 1000)
        page = self.db.list_collection_probe_entries(
            parent_id, offset=20, limit=40, query='video 02', sort_column='title'
        )
        self.assertEqual(len(page), 40)
        self.assertTrue(all('Video 02' in entry['title'] for entry in page))
        self.assertEqual(
            self.db.collection_probe_entry_count(parent_id, query='video 02'), 100
        )
        selected_before = self.db.count_selected_collection_probe_entries(parent_id)
        self.db.set_collection_probe_entry_selected(parent_id, 3, True)
        self.assertEqual(
            self.db.count_selected_collection_probe_entries(parent_id), selected_before + 1
        )
        summary = self.db.collection_probe_storage_summary(parent_id)
        self.assertEqual(summary['selected_count'], selected_before + 1)
        self.assertGreater(summary['estimated_bytes'], 0)

        self.db.close()
        self.db = Database(Path(self.temp.name) / 'app.db')
        self.service.db = self.db
        self.assertEqual(self.db.collection_probe_entry_count(parent_id), 1000)

    def test_collection_entry_materialization_is_idempotent_after_restart_boundary(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list-idempotent', self.temp.name, title='List'
        )
        entries = [
            {'url': 'https://example.test/1', 'source_key': 'generic:1', 'title': 'One', 'index': 1},
            {'url': 'https://example.test/2', 'source_key': 'generic:2', 'title': 'Two', 'index': 2},
        ]
        first = self.service.enqueue_collection_entries(parent_id, entries)
        second = self.service.enqueue_collection_entries(parent_id, entries)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(len(self.service.collection_children(parent_id)), 2)

    def test_collection_materialization_deduplicates_by_link_and_video_name(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/list-duplicates', self.temp.name, title='Duplicates'
        )
        created = self.service.enqueue_collection_entries(parent_id, [
            {
                'url': 'https://EXAMPLE.test/video/1/#fragment',
                'source_key': 'generic:first',
                'title': 'First video',
                'index': 1,
            },
            {
                'url': 'https://example.test/video/1',
                'source_key': 'generic:alias',
                'title': 'Alias title',
                'index': 20,
            },
            {
                'url': 'https://example.test/video/2',
                'source_key': 'generic:second',
                'title': '  SAME   NAME  ',
                'index': 2,
            },
            {
                'url': 'https://example.test/video/3',
                'source_key': 'generic:third',
                'title': 'same name',
                'index': 3,
            },
        ])

        self.assertEqual(len(created), 2)
        self.assertEqual(
            {self.service.tasks[task_id].url for task_id in created},
            {'https://EXAMPLE.test/video/1/#fragment', 'https://example.test/video/2'},
        )

    def test_collection_materialization_skips_video_active_in_another_task(self) -> None:
        active_id = self.service.enqueue(
            'https://example.test/already-active',
            self.temp.name,
            source_key='generic:active',
            start_immediately=False,
        )
        self.service.tasks[active_id].title = 'Already active video'
        parent_id = self.service.create_collection(
            'https://example.test/list-cross-parent', self.temp.name, title='Cross parent'
        )

        created = self.service.enqueue_collection_entries(parent_id, [{
            'url': 'https://example.test/different-alias',
            'source_key': 'generic:active',
            'title': 'Different title',
            'index': 1,
        }])

        self.assertEqual(created, [])
        self.assertEqual(len(self.service.collection_children(parent_id)), 0)

    def test_collection_materialization_normalizes_source_keys_and_tolerates_bad_indexes(self) -> None:
        self.service.enqueue(
            'https://example.test/already-active-case',
            self.temp.name,
            source_key='YouTube:CaseSensitiveId',
            start_immediately=False,
        )
        parent_id = self.service.create_collection(
            'https://example.test/list-normalized', self.temp.name, title='Normalized'
        )

        created = self.service.enqueue_collection_entries(parent_id, [
            {
                'url': 'https://example.test/different-alias',
                'source_key': 'youtube:CaseSensitiveId',
                'title': 'Duplicate by normalized extractor',
                'index': 1,
            },
            {
                'url': 'https://example.test/new-video',
                'source_key': 'Generic:new-video',
                'title': 'New video',
                'index': 'invalid-index',
                'entry_kind': 'Video',
            },
        ])

        self.assertEqual(len(created), 1)
        child = self.service.tasks[created[0]]
        self.assertEqual(child.source_key, 'generic:new-video')
        self.assertEqual(child.collection_index, 2)

    def test_large_collection_materializes_in_bounded_batches_and_resumes_after_restart(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/large-materialization', self.temp.name, title='Large'
        )
        entries = [
            {
                'index': index,
                'url': f'https://example.test/video/{index}',
                'source_key': f'generic:{index}',
                'title': f'Video {index}',
                'entry_kind': 'video',
                'downloadable': True,
                'selected': True,
            }
            for index in range(1, 451)
        ]
        self.db.upsert_collection_probe_entries(parent_id, entries)
        emitted_sizes: list[int] = []
        self.service.tasks_added.connect(lambda batch: emitted_sizes.append(len(batch)))
        self.assertTrue(self.service.start_collection_materialization(parent_id, 'original'))
        self.service._materialization_timer.stop()
        self.service._process_collection_materialization_batch()
        self.service._materialization_timer.stop()
        self.service._materialization_parents.clear()
        self.assertEqual(len(self.service.collection_children(parent_id)), 200)
        self.assertEqual(emitted_sizes, [200])

        restored = DownloadService(self.db)
        restored._start_next = lambda: None
        restored_sizes: list[int] = []
        restored.tasks_added.connect(lambda batch: restored_sizes.append(len(batch)))
        restored.restore_tasks()
        deadline = time.monotonic() + 2.0
        while len(restored.collection_children(parent_id)) < 450 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        restored._materialization_timer.stop()

        self.assertEqual(len(restored.collection_children(parent_id)), 450)
        self.assertEqual(restored_sizes, [200, 50])
        state = restored.tasks[parent_id].options_json['_collection_materialization']
        self.assertFalse(state['active'])
        self.assertEqual(state['offset'], 450)

    def test_child_progress_coalesces_parent_collection_refreshes(self) -> None:
        parent_id = self.service.create_collection(
            'https://example.test/coalesced', self.temp.name, title='Coalesced'
        )
        child_id = self.service.enqueue_collection_entries(parent_id, [{
            'index': 1,
            'url': 'https://example.test/coalesced/1',
            'source_key': 'generic:coalesced-1',
            'title': 'One',
        }])[0]
        calls: list[str] = []
        original = self.service._refresh_collection
        self.service._refresh_collection = lambda task_id: calls.append(task_id)
        try:
            for percent in range(20):
                self.service._on_progress(child_id, {'percent': percent})
            deadline = time.monotonic() + 1.0
            while not calls and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
        finally:
            self.service._refresh_collection = original
        self.assertEqual(calls, [parent_id])

    def test_resume_index_maps_to_both_collection_probe_cores(self) -> None:
        request = CollectionProbeRequest(
            request_id='resume', url='https://example.test/list', resume_index=320
        )
        worker = CollectionProbeWorker(request)
        options = worker._probe_options()
        self.assertEqual(options['playliststart'], 321)
        command = build_external_ytdlp_command(
            'yt-dlp.exe', request.url, options, download=False
        )
        start = command.index('--playlist-start')
        self.assertEqual(command[start + 1], '321')

    def test_storage_preview_is_persisted_with_the_task(self) -> None:
        task_id = self.service.enqueue(
            'https://example.test/storage', self.temp.name, start_immediately=False
        )
        preview = {
            'known': True,
            'temporary_bytes': 900,
            'final_bytes': 600,
            'entry_count': 1,
            'merge_entry_count': 1,
            'temporary_dir': str(Path(self.temp.name) / 'temp'),
            'final_dir': self.temp.name,
            'cross_volume': False,
        }
        self.service._on_progress(task_id, {'storage_preview': preview})
        row = next(row for row in self.db.list_download_tasks() if row['id'] == task_id)
        self.assertEqual(json.loads(row['options_json'])['_storage_preview'], preview)

        restored = DownloadService(self.db)
        restored._start_next = lambda: None
        restored.restore_tasks()
        self.assertEqual(restored.tasks[task_id].options_json['_storage_preview'], preview)


class CollectionModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_disabled_rows_cannot_be_checked(self) -> None:
        model = CollectionEntryModel()
        model.append([
            {'title': 'ok', 'downloadable': True, 'selected': True},
            {'title': 'private', 'downloadable': False, 'selected': False, 'disabled_reason': 'private'},
        ])
        self.assertTrue(model.setData(model.index(0, 0), Qt.Unchecked, Qt.CheckStateRole))
        self.assertFalse(model.setData(model.index(1, 0), Qt.Checked, Qt.CheckStateRole))
        self.assertEqual(model.selected_entries(), [])

    def test_paged_model_loads_only_the_accessed_page_and_refreshes_in_database(self) -> None:
        model = CollectionEntryModel()
        calls: list[tuple[int, int, str]] = []

        def view_loader(offset, limit, view):
            calls.append((offset, limit, str(view.get('query') or '')))
            return [
                {'index': row + 1, 'title': f'Video {row + 1}', 'downloadable': True}
                for row in range(offset, min(offset + limit, 10_000))
            ]

        model.set_paged_source(
            10_000,
            lambda offset, limit: [],
            selection_updater=lambda _index, _selected: None,
            selection_setter=lambda _mode: None,
            selected_loader=lambda: [],
            selected_counter=lambda: 0,
            view_loader=view_loader,
            view_counter=lambda view: 25 if view.get('query') else 10_000,
        )
        self.assertEqual(model.entry_at(325), {})
        deadline = time.monotonic() + 1.0
        while model.entry_at(325) == {} and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertEqual(model.entry_at(325)['index'], 326)
        self.assertEqual(calls, [(320, 160, '')])
        self.assertEqual(model.entry_at(326)['index'], 327)
        self.assertEqual(len(calls), 1)
        model.set_paged_view(query='needle')
        deadline = time.monotonic() + 1.0
        while model.rowCount() != 25 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertEqual(model.rowCount(), 25)
        self.assertEqual(model.entry_at(0), {})
        deadline = time.monotonic() + 1.0
        while model.entry_at(0) == {} and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertEqual(model.entry_at(0)['index'], 1)
        self.assertEqual(calls[-1], (0, 160, 'needle'))

    def test_collection_search_is_debounced_and_only_queries_the_latest_text(self) -> None:
        page = CollectionSelectionPage()
        self.addCleanup(page.close)
        queries: list[str] = []
        page.model.set_paged_source(
            100,
            lambda _offset, _limit: [],
            selection_updater=lambda _index, _selected: None,
            selection_setter=lambda _mode: None,
            selected_loader=lambda: [],
            selected_counter=lambda: 0,
            view_loader=lambda _offset, _limit, _view: [],
            view_counter=lambda view: queries.append(str(view.get('query') or '')) or 0,
        )
        page.search.setText('a')
        page.search.setText('ab')
        page.search.setText('abc')
        deadline = time.monotonic() + 1.0
        while not queries and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertEqual(queries, ['abc'])

    def test_stale_background_count_cannot_replace_newer_filter_result(self) -> None:
        model = CollectionEntryModel()
        first_started = threading.Event()
        release_first = threading.Event()

        def counter(view) -> int:
            if view.get('query') == 'first':
                first_started.set()
                release_first.wait(1.0)
                return 99
            return 2

        model.set_paged_source(
            100,
            lambda _offset, _limit: [],
            selection_updater=lambda _index, _selected: None,
            selection_setter=lambda _mode: None,
            selected_loader=lambda: [],
            selected_counter=lambda: 0,
            view_loader=lambda _offset, _limit, _view: [],
            view_counter=counter,
        )
        model.set_paged_view(query='first')
        self.assertTrue(first_started.wait(0.5))
        model.set_paged_view(query='second')
        deadline = time.monotonic() + 1.0
        while model.rowCount() != 2 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        release_first.set()
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertEqual(model.rowCount(), 2)

    def test_collection_detail_updates_only_the_changed_child_row(self) -> None:
        page = CollectionDetailPage()
        parent = DownloadTask(
            'parent', 'https://example.test/list', 'D:/downloads',
            task_kind='collection', title='List', status='downloading',
        )
        children = [
            DownloadTask(
                f'child-{index}', f'https://example.test/{index}', 'D:/downloads',
                parent_task_id='parent', collection_index=index, title=f'Video {index}',
            )
            for index in range(1, 1001)
        ]
        page.set_collection(parent, children)
        resets: list[bool] = []
        changed_rows: list[int] = []
        page.model.modelReset.connect(lambda: resets.append(True))
        page.model.dataChanged.connect(lambda first, _last, _roles: changed_rows.append(first.row()))

        target = children[499]
        target.progress = 67
        target.status = 'downloading'
        page.upsert_task(target)

        self.assertEqual(resets, [])
        self.assertEqual(changed_rows, [499])
        self.assertEqual(page.model.data(page.model.index(499, 3)), '67.0%')

    def test_collection_detail_opens_the_selected_nested_collection_after_filtering(self) -> None:
        page = CollectionDetailPage()
        parent = DownloadTask(
            'parent', 'https://example.test/list', 'D:/downloads',
            task_kind='collection', title='List',
        )
        video = DownloadTask(
            'video', 'https://example.test/video', 'D:/downloads',
            parent_task_id=parent.id, collection_index=1, title='Video',
        )
        nested = DownloadTask(
            'nested', 'https://example.test/nested', 'D:/downloads',
            task_kind='collection', parent_task_id=parent.id,
            collection_index=2, title='Nested collection',
        )
        page.set_collection(parent, [video, nested])
        page.proxy.set_query('nested')
        self.assertEqual(page.proxy.rowCount(), 1)

        opened: list[str] = []
        page.nested_requested.connect(opened.append)
        page.table.selectRow(0)
        page._open_nested(page.proxy.index(0, 0))

        self.assertEqual(page.selected_task_id(), nested.id)
        self.assertEqual(opened, [nested.id])


if __name__ == '__main__':
    unittest.main()

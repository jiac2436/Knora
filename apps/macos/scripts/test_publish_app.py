"""Release safety checks use only temporary fake bundles, never real Apps."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import publish_app


class PublishAppTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.dist = self.root / 'apps/macos/dist'
        self.dist.mkdir(parents=True)
        (self.root / 'apps/macos/.build').mkdir()
        script = self.root / 'apps/macos/scripts/build.sh'
        script.parent.mkdir(); script.write_text('synthetic build script')
        self.old = self.dist / '知识收集箱.app'
        self.old.mkdir(); (self.old / 'marker').write_text('old')
        self.destination = self.dist / 'Knora.app'
        self.source = self.root / 'candidate.app'
        (self.source / 'Contents/MacOS').mkdir(parents=True)
        (self.source / 'Contents/MacOS/KnowledgeInbox').write_text('synthetic executable')
        (self.source / 'marker').write_text('new')
        self.record = self.root / 'record.json'
        self.record.write_text(json.dumps(dict(manifest_sha256='synthetic', schema_version=16,
            plugin_version='1.0.5', tests=dict(status='passed'))))

    def command(self, arguments, **kwargs):
        if arguments[0] == 'cp':
            shutil.copytree(arguments[2], arguments[3])
        return subprocess.CompletedProcess(arguments, 1 if arguments[0] == 'lsof' else 0, stdout='', stderr='')

    def run_publish(self, command=None):
        argv = ['publish_app.py', '--source', str(self.source), '--record', str(self.record)]
        with patch.object(publish_app, 'ROOT', self.root), patch.object(sys, 'argv', argv), \
                patch.object(publish_app, 'verify', return_value=1), \
                patch.object(publish_app.subprocess, 'run', side_effect=command or self.command), \
                contextlib.redirect_stdout(io.StringIO()):
            publish_app.main()

    def test_publish_preserves_previous_bundle(self):
        self.run_publish()
        self.assertEqual((self.destination / 'marker').read_text(), 'new')
        self.assertFalse(self.old.exists())
        release = json.loads((self.root / 'apps/macos/release.json').read_text())
        saved = self.root / release['archive'] / '知识收集箱.app'
        self.assertEqual((saved / 'marker').read_text(), 'old')
        self.assertEqual([p.name for p in self.dist.glob('*.app')], ['Knora.app'])

    def test_publish_replaces_an_existing_knora_bundle(self):
        self.old.rename(self.destination)
        self.run_publish()
        self.assertEqual((self.destination / 'marker').read_text(), 'new')
        release = json.loads((self.root / 'apps/macos/release.json').read_text())
        self.assertEqual((self.root / release['archive'] / 'Knora.app/marker').read_text(), 'old')
        self.assertEqual([p.name for p in self.dist.glob('*.app')], ['Knora.app'])

    def test_copy_failure_leaves_existing_app_untouched(self):
        def command(arguments, **kwargs):
            if arguments[0] == 'cp':
                raise OSError('synthetic copy failure')
            return self.command(arguments, **kwargs)
        with self.assertRaisesRegex(OSError, 'copy failure'):
            self.run_publish(command)
        self.assertEqual((self.old / 'marker').read_text(), 'old')
        self.assertFalse(self.destination.exists())

    def test_release_record_failure_restores_existing_app(self):
        original = publish_app.write_json
        def write(path, value):
            if path.name == 'release.json':
                raise OSError('synthetic record failure')
            original(path, value)
        with patch.object(publish_app, 'write_json', side_effect=write):
            with self.assertRaisesRegex(OSError, 'record failure'):
                self.run_publish()
        self.assertEqual((self.old / 'marker').read_text(), 'old')
        self.assertFalse(self.destination.exists())

    def test_running_app_blocks_publication(self):
        def command(arguments, **kwargs):
            if arguments[0] == 'lsof':
                return subprocess.CompletedProcess(arguments, 0, stdout='n/tmp/running.app/Contents/MacOS/KnowledgeInbox\n')
            return self.command(arguments, **kwargs)
        with self.assertRaisesRegex(ValueError, '先用 ⌘Q'):
            self.run_publish(command)
        self.assertEqual((self.old / 'marker').read_text(), 'old')

    def test_unverified_candidate_blocks_publication(self):
        self.record.write_text(json.dumps(dict(tests=dict(status='verification_in_progress'))))
        with self.assertRaisesRegex(ValueError, '验收记录尚未通过'):
            self.run_publish()
        self.assertEqual((self.old / 'marker').read_text(), 'old')


if __name__ == '__main__':
    unittest.main()

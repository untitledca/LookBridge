#!/usr/bin/env python3
"""Exercise real Sparkle download, signatures, replacement and relaunch locally."""
import argparse
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--archive-format', choices=['dmg', 'zip'], default='dmg')
parser.add_argument('--scenario', choices=['all', 'valid-update', 'tampered-archive', 'tampered-feed'], default='all')
options = parser.parse_args()
SDK = ROOT / '.build/dependencies/Sparkle-2.10.0'
ACCOUNT = os.environ.get('LOOKBRIDGE_SPARKLE_KEY_ACCOUNT', 'com.atomfilmlab.lookbridge.sparkle')
PUBLIC_KEY = plistlib.loads((ROOT / 'macos/Resources/Info.plist').read_bytes())['SUPublicEDKey']
WORK = Path(tempfile.mkdtemp(prefix='lookbridge-installer-smoke-'))
PROOF = ROOT / '.build' / ('automatic-installer-smoke-' + time.strftime('%Y%m%d-%H%M%S'))
PROOF.mkdir(parents=True)


def run(*args, **kwargs):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
    return result


class Server(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        with (PROOF / 'http-requests.log').open('a') as out:
            out.write((fmt % args) + '\n')


server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Server, directory=str(WORK)))
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_port
binary = WORK / 'AutomaticInstallerSmoke'
run('xcrun', 'swiftc', '-swift-version', '5', '-O', '-target', platform.machine() + '-apple-macos12.0',
    '-F', SDK, '-framework', 'Sparkle', '-Xlinker', '-rpath', '-Xlinker', '@executable_path/../Frameworks',
    ROOT / 'macos/Sources/AppUpdater.swift', ROOT / 'macos/Sources/AppModel.swift', ROOT / 'macos/Sources/ScriptInstaller.swift',
    ROOT / 'macos/tests/AutomaticInstallerSmoke.swift', '-o', binary)


def make_app(destination, identifier, version, log_path, feed, failure):
    (destination / 'Contents/MacOS').mkdir(parents=True)
    (destination / 'Contents/Frameworks').mkdir()
    shutil.copyfile(binary, destination / 'Contents/MacOS/AutomaticInstallerSmoke')
    (destination / 'Contents/Resources/Scripts').mkdir(parents=True)
    for script in (ROOT / 'scripts').glob('*.py'):
        shutil.copyfile(script, destination / 'Contents/Resources/Scripts' / script.name)
    (destination / 'Contents/MacOS/AutomaticInstallerSmoke').chmod(0o755)
    run('ditto', '--noextattr', '--norsrc', SDK / 'Sparkle.framework', destination / 'Contents/Frameworks/Sparkle.framework')
    info = dict(CFBundleIdentifier=identifier, CFBundleName='LookBridge Updater Smoke', CFBundleDisplayName='LookBridge Updater Smoke',
                CFBundleExecutable='AutomaticInstallerSmoke', CFBundlePackageType='APPL', CFBundleVersion=version,
                CFBundleShortVersionString=version + '.0', LSMinimumSystemVersion='12.0', LSUIElement=True,
                SUFeedURL=feed, SUPublicEDKey=PUBLIC_KEY, SUEnableAutomaticChecks=False,
                SUAutomaticallyUpdate=False, SUEnableSystemProfiling=False,
                SUVerifyUpdateBeforeExtraction=True, SURequireSignedFeed=True,
                SUSignedFeedFailureExpirationInterval=0,
                NSAppTransportSecurity={'NSAllowsArbitraryLoads': True},
                SmokeLogPath=str(log_path), SmokeExpectedFailure=failure, SmokeRoot=str(log_path.parent / log_path.stem))
    (destination / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    run('codesign', '--force', '--sign', '-', destination)
    run('codesign', '--verify', '--deep', '--strict', destination)


def records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def scenario(name, tamper_archive=False, tamper_feed=False):
    expected_failure = tamper_archive or tamper_feed
    case = WORK / name
    feed_dir = case / 'feed'
    feed_dir.mkdir(parents=True)
    installed = case / 'installed' / 'LookBridge Updater Smoke.app'
    replacement = case / 'replacement' / 'LookBridge Updater Smoke.app'
    log = PROOF / (name + '.jsonl')
    identifier = 'com.atomfilmlab.lookbridge.smoke.' + uuid.uuid4().hex
    feed = f'http://127.0.0.1:{port}/{name}/feed/appcast.xml'
    make_app(installed, identifier, '1', log, feed, expected_failure)
    make_app(replacement, identifier, '2', log, feed, expected_failure)
    archive = feed_dir / ('LookBridge-Updater-Smoke-2.0.' + options.archive_format)
    if options.archive_format == 'dmg':
        # Match make-dmg.sh: one app, Applications link and installation text,
        # packaged as an HFS+ read-only compressed disk image.
        layout = case / 'dmg-layout'
        layout.mkdir()
        run('ditto', '--noextattr', '--norsrc', replacement, layout / replacement.name)
        (layout / 'Applications').symlink_to('/Applications')
        (layout / 'Install.txt').write_text('Isolated LookBridge automatic installer test.\n')
        run('hdiutil', 'create', '-volname', 'LookBridge Updater Smoke', '-srcfolder', layout,
            '-fs', 'HFS+', '-format', 'UDZO', '-imagekey', 'zlib-level=9', '-ov', archive)
        run('hdiutil', 'verify', archive)
    else:
        run('ditto', '-c', '-k', '--keepParent', replacement, archive)
    run(SDK / 'bin/generate_appcast', '--account', ACCOUNT, '--maximum-deltas', '0',
        '--download-url-prefix', f'http://127.0.0.1:{port}/{name}/feed/', feed_dir)
    original_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if tamper_archive:
        with archive.open('r+b') as file:
            file.seek(128)
            byte = file.read(1)
            file.seek(128)
            file.write(bytes([byte[0] ^ 0x01]))
        assert hashlib.sha256(archive.read_bytes()).hexdigest() != original_digest
    if tamper_feed:
        appcast = feed_dir / 'appcast.xml'
        original = appcast.read_bytes()
        changed = original.replace(b'<title>2.0</title>', b'<title>9.0</title>')
        assert changed != original and len(changed) == len(original)
        appcast.write_bytes(changed)
    shutil.copyfile(feed_dir / 'appcast.xml', PROOF / (name + '-appcast.xml'))
    with (PROOF / (name + '-process.log')).open('w') as output:
        process = subprocess.Popen([str(installed / 'Contents/MacOS/AutomaticInstallerSmoke')], stdout=output, stderr=output)
        deadline = time.monotonic() + 110
        while time.monotonic() < deadline:
            events = records(log)
            if any(x['event'] == 'scriptsRefreshed' for x in events):
                break
            if any(x['event'] in ('updateRejected', 'timedOut') for x in events):
                break
            time.sleep(0.25)
        events = records(log)
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    disk_version = plistlib.loads((installed / 'Contents/Info.plist').read_bytes())['CFBundleVersion']
    if expected_failure:
        rejected = next((x for x in events if x['event'] == 'updateRejected'), None)
        assert rejected, events
        assert 'sign' in rejected['error'].lower() or 'validat' in rejected['error'].lower(), rejected
        assert not any(x['event'] == 'downloadValidatedAndPrepared' for x in events), events
        assert not any(x['event'] == 'replacementRelaunched' for x in events), events
        assert disk_version == '1', disk_version
        if tamper_feed:
            assert not any(x.get('state') == 'downloading' for x in events), events
    else:
        for expected in ['downloadValidatedAndPrepared', 'windowDeferralVerified', 'resolveDeferralVerified', 'safeToInstall', 'replacementRelaunched', 'scriptRefreshDeferred', 'scriptsRefreshed']:
            assert any(x['event'] == expected for x in events), (expected, events)
        for event in events:
            if event['event'] in ('windowDeferralVerified', 'resolveDeferralVerified'):
                assert event['pending'] and event['stillOriginalVersion'], event
        assert disk_version == '2', disk_version
        deferred = next(x for x in events if x['event'] == 'scriptRefreshDeferred')
        refreshed = next(x for x in events if x['event'] == 'scriptsRefreshed')
        assert deferred['build'] == '1' and not deferred['installed'], deferred
        assert refreshed['build'] == '2' and refreshed['installed'], refreshed
        first_pid = next(x['pid'] for x in events if x['event'] == 'launched' and x['version'] == '1')
        new_pid = next(x['pid'] for x in events if x['event'] == 'replacementRelaunched')
        assert first_pid != new_pid
        run('codesign', '--verify', '--deep', '--strict', installed)
    result = dict(scenario=name, passed=True, archiveFormat=options.archive_format,
                  finalVersion=disk_version, appPath=str(installed), log=str(log),
                  originalArchiveSHA256=original_digest, servedArchiveSHA256=hashlib.sha256(archive.read_bytes()).hexdigest())
    print(json.dumps(result), flush=True)
    return result


try:
    results = []
    if options.scenario in ('all', 'valid-update'):
        results.append(scenario('valid-update'))
    if options.scenario in ('all', 'tampered-archive'):
        results.append(scenario('tampered-archive', tamper_archive=True))
    if options.scenario in ('all', 'tampered-feed'):
        results.append(scenario('tampered-feed', tamper_feed=True))
    (PROOF / 'result.json').write_text(json.dumps(dict(passed=True, workspace=str(WORK), results=results), indent=2) + '\n')
    print('PASS: selected Sparkle integration scenarios (' + options.scenario + '). Proof: ' + str(PROOF), flush=True)
finally:
    server.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)

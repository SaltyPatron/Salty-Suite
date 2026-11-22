#!/usr/bin/env python3
"""Comprehensive unit tests for machine_env.py and its launchers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
PKG_DIR = HERE.parent
if str(PKG_DIR) not in sys.path:
    sys.path.insert(0, str(PKG_DIR))

import machine_env  # noqa: E402

MACHINE_ENV_PY = PKG_DIR / 'machine_env.py'
MACHINE_ENV_CMD = PKG_DIR / 'machine-env.cmd'
MACHINE_ENV_PS1 = PKG_DIR / 'machine-env.ps1'


def run_cli(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy() if env is None else dict(env)
    return subprocess.run(
        [sys.executable, str(MACHINE_ENV_PY), *args],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=15.0,
    )


class MachineEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parsing_paths_quotes_comments_and_bom(self) -> None:
        env_file = self.work / 'machine.env'
        content = (
            '\ufeff'
            '# leading comment\n'
            '   # indented comment with "unmatched quote\n'
            '\n'
            'APP_ROOT=C:\\Program Files\\MyApp\n'
            'export APP_DATA="D:\\Data\\Store\\"\n'
            "APP_LABEL='plain text # hash'\n"
            'APP_EMPTY=\n'
            'APP_QUOTED_EMPTY=""\n'
            'APP_INLINE=value # trailing comment\n'
        )
        env_file.write_text(content, encoding='utf-8')

        resolved = machine_env.resolve_env(env_file, {}, process_precedence=True)
        self.assertEqual(resolved, {
            'APP_ROOT': 'C:\\Program Files\\MyApp',
            'APP_DATA': 'D:\\Data\\Store\\',
            'APP_LABEL': 'plain text # hash',
            'APP_EMPTY': '',
            'APP_QUOTED_EMPTY': '',
            'APP_INLINE': 'value',
        })

    def test_variable_expansion_forms(self) -> None:
        env_file = self.work / 'expand.env'
        env_file.write_text(
            'HOST=127.0.0.1\n'
            'PORT=8080\n'
            'WIN_BASE=C:\\Srv\\App\n'
            'URL_CURLY=http://${HOST}:${PORT}/api\n'
            'URL_BARE=http://$HOST:$PORT/health\n'
            'LOG_DIR=%WIN_BASE%\\logs\n'
            'FROM_PROC=${EXT_TAG}_ok\n',
            encoding='utf-8',
        )
        resolved = machine_env.resolve_env(
            env_file,
            {'EXT_TAG': 'blue'},
            process_precedence=True,
        )
        self.assertEqual(resolved['URL_CURLY'], 'http://127.0.0.1:8080/api')
        self.assertEqual(resolved['URL_BARE'], 'http://127.0.0.1:8080/health')
        self.assertEqual(resolved['LOG_DIR'], 'C:\\Srv\\App\\logs')
        self.assertEqual(resolved['FROM_PROC'], 'blue_ok')

    def test_process_precedence_on_and_off(self) -> None:
        env_file = self.work / 'prec.env'
        env_file.write_text(
            'PORT=8080\n'
            'HOST=0.0.0.0\n'
            'EMPTY_PROC=fallback\n'
            'ENDPOINT=http://${HOST}:${PORT}\n',
            encoding='utf-8',
        )
        proc = {'PORT': '9090', 'HOST': '127.0.0.1', 'EMPTY_PROC': ''}

        res_on = run_cli(['--file', str(env_file), '--process-precedence', 'on', '--format', 'json'], env=proc)
        self.assertEqual(res_on.returncode, 0, msg=res_on.stderr)
        data_on = json.loads(res_on.stdout)
        self.assertEqual(data_on['PORT'], '9090')
        self.assertEqual(data_on['HOST'], '127.0.0.1')
        self.assertEqual(data_on['EMPTY_PROC'], 'fallback')
        self.assertEqual(data_on['ENDPOINT'], 'http://127.0.0.1:9090')

        res_off = run_cli(['--file', str(env_file), '--process-precedence', 'off', '--format', 'json'], env=proc)
        self.assertEqual(res_off.returncode, 0, msg=res_off.stderr)
        data_off = json.loads(res_off.stdout)
        self.assertEqual(data_off['PORT'], '8080')
        self.assertEqual(data_off['HOST'], '0.0.0.0')
        self.assertEqual(data_off['ENDPOINT'], 'http://0.0.0.0:8080')

    def test_duplicate_nul_undefined_and_syntax_errors(self) -> None:
        dup_exact = self.work / 'dup1.env'
        dup_exact.write_text('A=1\nA=2\n', encoding='utf-8')
        with self.assertRaises(machine_env.ConfigError):
            machine_env.resolve_env(dup_exact, {})

        dup_case = self.work / 'dup2.env'
        dup_case.write_text('Port=1\nPORT=2\n', encoding='utf-8')
        with self.assertRaises(machine_env.ConfigError):
            machine_env.resolve_env(dup_case, {})

        nul_file = self.work / 'nul.env'
        nul_file.write_bytes(b'KEY=abc\x00def\n')
        with self.assertRaises(machine_env.ConfigError):
            machine_env.resolve_env(nul_file, {})

        undef_file = self.work / 'undef.env'
        undef_file.write_text('A=${NO_SUCH_KEY}\n', encoding='utf-8')
        with self.assertRaises(machine_env.ConfigError):
            machine_env.resolve_env(undef_file, {})

        bad_line = self.work / 'bad.env'
        bad_line.write_text('not an assignment\n', encoding='utf-8')
        res = run_cli(['--file', str(bad_line)])
        self.assertEqual(res.returncode, 1)

    def test_strict_and_source_key_behavior(self) -> None:
        missing = self.work / 'absent.env'

        # Non-strict missing file returns empty dict (plus optional source key = 'defaults').
        res_default = run_cli(['--file', str(missing), '--format', 'json'], env={})
        self.assertEqual(res_default.returncode, 0, msg=res_default.stderr)
        self.assertEqual(json.loads(res_default.stdout), {})

        res_src_default = run_cli(
            ['--file', str(missing), '--source-key', 'CONFIG_SOURCE', '--format', 'json'],
            env={},
        )
        self.assertEqual(res_src_default.returncode, 0)
        self.assertEqual(json.loads(res_src_default.stdout), {'CONFIG_SOURCE': 'defaults'})

        # Strict missing file fails.
        res_strict = run_cli(['--file', str(missing), '--strict'])
        self.assertEqual(res_strict.returncode, 1)

        # Existing file with --source-key records resolved path; omitted --source-key adds nothing.
        present = self.work / 'present.env'
        present.write_text('APP_NAME=svc\n', encoding='utf-8')
        res_no_src = run_cli(['--file', str(present), '--format', 'json'], env={})
        self.assertEqual(json.loads(res_no_src.stdout), {'APP_NAME': 'svc'})

        res_with_src = run_cli(
            ['--file', str(present), '--source-key', 'META_SOURCE', '--format', 'json'],
            env={},
        )
        self.assertEqual(json.loads(res_with_src.stdout), {
            'APP_NAME': 'svc',
            'META_SOURCE': str(present.resolve()),
        })

        # Invalid source-key fails misuse with exit code 2.
        bad_src = run_cli(['--file', str(present), '--source-key', '123-BAD'])
        self.assertEqual(bad_src.returncode, 2)

    def test_schema_validation_defaults_and_errors(self) -> None:
        schema_path = self.work / 'schema.json'
        data_dir = self.work / 'data'
        data_dir.mkdir()
        schema_path.write_text(json.dumps({
            'additionalProperties': False,
            'properties': {
                'APP_PORT': {'type': 'integer', 'required': True, 'min': 1, 'max': 65535},
                'APP_TLS': {'type': 'bool', 'default': False},
                'APP_DATA': {'type': 'path', 'required': True, 'absolute': True},
                'APP_ENV': {'type': 'enum', 'values': ['dev', 'staging', 'prod'], 'default': 'dev'},
                'APP_TAG': {'type': 'string', 'default': 'svc-${APP_ENV}'},
            },
        }), encoding='utf-8')

        valid_env = self.work / 'valid.env'
        valid_env.write_text(
            f'APP_PORT=8443\nAPP_DATA={data_dir}\n',
            encoding='utf-8',
        )
        res = run_cli(['--file', str(valid_env), '--schema', str(schema_path), '--format', 'json'], env={})
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertEqual(json.loads(res.stdout), {
            'APP_PORT': '8443',
            'APP_DATA': str(data_dir),
            'APP_TLS': 'false',
            'APP_ENV': 'dev',
            'APP_TAG': 'svc-dev',
        })

        invalid_cases = [
            ('missing_required', 'APP_PORT=8080\n'),
            ('bad_integer', f'APP_PORT=80.5\nAPP_DATA={data_dir}\n'),
            ('out_of_range', f'APP_PORT=70000\nAPP_DATA={data_dir}\n'),
            ('bad_bool', f'APP_PORT=8080\nAPP_DATA={data_dir}\nAPP_TLS=maybe\n'),
            ('bad_enum', f'APP_PORT=8080\nAPP_DATA={data_dir}\nAPP_ENV=qa\n'),
            ('relative_path', 'APP_PORT=8080\nAPP_DATA=relative/path\n'),
            ('unknown_key', f'APP_PORT=8080\nAPP_DATA={data_dir}\nEXTRA=1\n'),
        ]
        for label, body in invalid_cases:
            with self.subTest(case=label):
                bad_env = self.work / f'{label}.env'
                bad_env.write_text(body, encoding='utf-8')
                bad_res = run_cli(['--file', str(bad_env), '--schema', str(schema_path)], env={})
                self.assertEqual(bad_res.returncode, 1, msg=f'{label} unexpectedly succeeded')

    def test_all_five_output_formats(self) -> None:
        env_file = self.work / 'formats.env'
        env_file.write_text(
            'PORT=8080\n'
            'PATH_WIN=C:\\App\\bin\n'
            'DOUBLE="a & b <c> \'single\' 100%"\n'
            'SINGLE=\'x & y "quoted" 50%\'\n',
            encoding='utf-8',
        )

        # 1. shell
        sh_res = run_cli(['--file', str(env_file), '--format', 'shell'], env={})
        self.assertEqual(sh_res.returncode, 0, msg=sh_res.stderr)
        lines = sh_res.stdout.strip().splitlines()
        self.assertEqual(lines, [
            'export PORT=8080',
            "export PATH_WIN='C:\\App\\bin'",
            "export DOUBLE='a & b <c> '\"'\"'single'\"'\"' 100%'",
            "export SINGLE='x & y \"quoted\" 50%'",
        ])

        # 2. json
        json_res = run_cli(['--file', str(env_file), '--format', 'json'], env={})
        self.assertEqual(json_res.returncode, 0)
        parsed_json = json.loads(json_res.stdout)
        self.assertEqual(parsed_json, {
            'PORT': '8080',
            'PATH_WIN': 'C:\\App\\bin',
            'DOUBLE': "a & b <c> 'single' 100%",
            'SINGLE': 'x & y "quoted" 50%',
        })

        # 3. systemd
        sys_res = run_cli(['--file', str(env_file), '--format', 'systemd'], env={})
        self.assertEqual(sys_res.returncode, 0, msg=sys_res.stderr)
        sys_lines = sys_res.stdout.strip().splitlines()
        self.assertEqual(sys_lines, [
            'Environment="PORT=8080"',
            'Environment="PATH_WIN=C:\\\\App\\\\bin"',
            'Environment="DOUBLE=a & b <c> \'single\' 100%%"',
            'Environment="SINGLE=x & y \\"quoted\\" 50%%"',
        ])

        # 4. iis
        iis_res = run_cli(['--file', str(env_file), '--format', 'iis'], env={})
        self.assertEqual(iis_res.returncode, 0, msg=iis_res.stderr)
        iis_root = ET.fromstring(iis_res.stdout)
        self.assertEqual(iis_root.tag, 'environmentVariables')
        iis_map = {elem.attrib['name']: elem.attrib['value'] for elem in iis_root.findall('add')}
        self.assertEqual(iis_map, parsed_json)

        # 5. msbuild
        msb_res = run_cli(['--file', str(env_file), '--format', 'msbuild'], env={})
        self.assertEqual(msb_res.returncode, 0, msg=msb_res.stderr)
        msb_root = ET.fromstring(msb_res.stdout)
        self.assertEqual(msb_root.tag, 'Project')
        pg = msb_root.find('PropertyGroup')
        self.assertIsNotNone(pg)
        assert pg is not None
        msb_map = {child.tag: (child.text or '') for child in pg}
        self.assertEqual(msb_map, parsed_json)

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher test')
    def test_windows_cmd_and_ps1_launchers(self) -> None:
        env_file = self.work / 'launcher.env'
        env_file.write_text('MODE=staging\nPORT=9000\n', encoding='utf-8')

        res_cmd = subprocess.run(
            ['cmd.exe', '/c', str(MACHINE_ENV_CMD), '--file', str(env_file), '--format', 'json'],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res_cmd.returncode, 0, msg=res_cmd.stderr)
        self.assertEqual(json.loads(res_cmd.stdout), {'MODE': 'staging', 'PORT': '9000'})

        res_ps1 = subprocess.run(
            [
                'pwsh', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(MACHINE_ENV_PS1),
                '--file', str(env_file), '--format', 'json',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res_ps1.returncode, 0, msg=res_ps1.stderr)
        self.assertEqual(json.loads(res_ps1.stdout), {'MODE': 'staging', 'PORT': '9000'})


if __name__ == '__main__':
    unittest.main()

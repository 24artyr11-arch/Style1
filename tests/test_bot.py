import base64
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from bot import (
    AI,
    App,
    OUTFIT_IMAGE_PROMPT,
    TRY_ON_PROMPT,
    UpdateDispatcher,
    has_dedicated_mount,
    require_persistent_storage,
)
from core import QUESTIONS, Store, UserError, normalize_photo, validate_outfits


def photo(color='#789b85', image_format='JPEG'):
    result = io.BytesIO()
    Image.new('RGB', (500, 600), color).save(result, image_format)
    return result.getvalue()


class FakeTG:
    def __init__(self):
        self.sent = []
        self.calls = []

    def say(self, uid, text, buttons=None):
        self.sent.append(('text', uid, text, buttons))

    def photo(self, uid, raw, caption, buttons=None):
        Image.open(io.BytesIO(raw)).verify()
        self.sent.append(('photo', uid, caption, buttons))

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True

    def download(self, file_id):
        return photo()


class FakeAI:
    def __init__(self):
        self.classify_calls = 0
        self.outfit_photo_calls = 0
        self.try_on_calls = 0

    def classify(self, raw):
        self.classify_calls += 1
        return {
            'valid': True,
            'category': 'Верх',
            'item_type': 'Футболка',
            'description': 'Зелёный верх',
        }

    def outfits(self, items, answers):
        tops = [item['id'] for item in items if item['category'] == 'Верх']
        bottom = next(item['id'] for item in items if item['category'] == 'Низ')
        shoes = next(item['id'] for item in items if item['category'] == 'Обувь')
        return validate_outfits(
            {
                'outfits': [
                    {'title': 'Комплект', 'reason': 'Пояснение', 'ids': [top, bottom, shoes]}
                    for top in tops[:3]
                ]
            },
            items,
        )

    def outfit_photo(self, items):
        self.outfit_photo_calls += 1
        return photo('#ddeeff')

    def try_on(self, person_photo, items):
        self.try_on_calls += 1
        return photo('#ffeedd')


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / 'db')
        self.t, self.ai = FakeTG(), FakeAI()
        self.app = App(self.s, self.t, self.ai)

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def seed(self, uid=10):
        return [
            self.s.add(uid, str(index), category, category, photo(), item_type=category)
            for index, category in enumerate(['Верх'] * 3 + ['Низ', 'Обувь'])
        ]

    def event(self, uid, data=None, pics=None):
        message = {
            'message_id': 123,
            'chat': {'id': uid, 'type': 'private'},
            'from': {'id': uid},
            'text': '/start',
        }
        if pics:
            message['photo'] = pics
        if data:
            return {
                'callback_query': {
                    'id': 'x',
                    'from': {'id': uid},
                    'message': message,
                    'data': data,
                }
            }
        return {'message': message}

    def test_full_wizard_generates_images_and_swap_regenerates_one(self):
        ids = self.seed()
        self.app.handle(self.event(10, 'looks'))
        for step in range(len(QUESTIONS)):
            nonce = self.s.state(10)['nonce']
            self.app.handle(self.event(10, f'q:{nonce}:{step}:0'))
        self.assertEqual(self.ai.outfit_photo_calls, 3)
        self.assertEqual(sum(item[0] == 'photo' for item in self.t.sent), 3)
        outfit_ids = self.s.state(10)['recent']
        second = self.s.outfit(10, outfit_ids[1])
        self.app.handle(self.event(10, f'pick:{outfit_ids[0]}:{ids[0]}:{ids[1]}'))
        self.assertEqual(self.ai.outfit_photo_calls, 4)
        self.assertIn(ids[1], self.s.outfit(10, outfit_ids[0])['ids'])
        self.assertEqual(second, self.s.outfit(10, outfit_ids[1]))

    def test_cross_user_access_denied(self):
        ids = self.seed()
        outfit_id = self.s.save_outfit(10, {'ids': ids[:1] + ids[3:]}, photo())
        operations = [
            lambda: self.s.item(20, ids[0]),
            lambda: self.s.delete(20, ids[0]),
            lambda: self.s.outfit(20, outfit_id),
            lambda: self.s.replacement(20, outfit_id, ids[0], ids[1]),
        ]
        for operation in operations:
            with self.assertRaises(UserError):
                operation()

    def test_start_has_no_consent_gate_and_upload_only_classifies(self):
        self.app.handle(self.event(99))
        self.assertIn('Твой гардероб', self.t.sent[-1][2])
        self.app.handle(self.event(99, 'upload'))
        picture = [{'file_id': 'x', 'file_unique_id': 'x'}]
        self.app.handle(self.event(99, pics=picture))
        self.assertEqual(self.ai.classify_calls, 1)
        self.assertEqual(self.ai.outfit_photo_calls, 0)
        item = self.s.item(99, self.s.items(99)[0]['id'])
        self.assertIsNone(item['display_photo'])
        self.assertIsNotNone(item['photo'])
        tables = {row[0] for row in self.s.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('users', tables)

    def test_album_photos_independent_and_duplicate_free(self):
        self.app.callback(10, 'upload')
        for name in ('a', 'b', 'a'):
            self.app.handle(self.event(10, pics=[{'file_id': name, 'file_unique_id': name}]))
        self.assertEqual(len(self.s.items(10)), 2)
        self.assertEqual(self.ai.classify_calls, 2)
        self.assertEqual(self.ai.outfit_photo_calls, 0)

    def test_unsolicited_photo_asks_for_upload_type(self):
        self.app.handle(self.event(10, pics=[{'file_id': 'x', 'file_unique_id': 'x'}]))
        self.assertFalse(self.s.items(10))
        self.assertIn('Сначала выбери', self.t.sent[-1][2])

    def test_person_photo_flow_does_not_call_ai(self):
        self.app.callback(10, 'personadd')
        self.app.handle(self.event(10, pics=[{'file_id': 'person', 'file_unique_id': 'person'}]))
        self.assertIsNotNone(self.s.person_photo(10))
        self.assertEqual(self.ai.classify_calls, 0)
        self.assertEqual(self.ai.outfit_photo_calls, 0)
        self.assertEqual(self.ai.try_on_calls, 0)

    def test_try_on_is_cached_until_person_photo_changes(self):
        ids = self.seed()
        self.s.set_person_photo(10, photo('#111111'))
        outfit_id = self.s.save_outfit(
            10,
            {'title': 'Образ', 'reason': 'Причина', 'ids': [ids[0], ids[3], ids[4]], 'preferences': {}},
            photo(),
        )
        self.app.callback(10, f'tryon:{outfit_id}')
        self.app.callback(10, f'tryon:{outfit_id}')
        self.assertEqual(self.ai.try_on_calls, 1)
        self.s.set_person_photo(10, photo('#222222'))
        self.assertIsNone(self.s.outfit(10, outfit_id)['tryon_photo'])
        self.app.callback(10, f'tryon:{outfit_id}')
        self.assertEqual(self.ai.try_on_calls, 2)

    def test_stale_wizard_cannot_trigger_paid_call(self):
        self.seed()
        self.app.callback(10, 'looks')
        nonce = self.s.state(10)['nonce']
        self.app.callback(10, f'q:{nonce}:0:0')
        with self.assertRaises(UserError):
            self.app.callback(10, f'q:{nonce}:0:0')

    def test_callback_deletes_previous_message(self):
        self.app.handle(self.event(10, 'menu'))
        self.assertTrue(
            any(
                args
                and args[0] == 'deleteMessage'
                and kwargs == {}
                and args[1] == {'chat_id': 10, 'message_id': 123}
                for args, kwargs in self.t.calls
            )
        )

    def test_wizard_back_returns_to_previous_question(self):
        self.seed()
        self.app.callback(10, 'looks')
        nonce = self.s.state(10)['nonce']
        self.app.callback(10, f'q:{nonce}:0:0')
        self.assertEqual(self.s.state(10)['step'], 1)
        self.app.callback(10, f'qback:{nonce}:1')
        self.assertEqual(self.s.state(10)['step'], 0)
        self.assertNotIn(QUESTIONS[0][0], self.s.state(10)['answers'])

    def test_one_or_two_outfits_are_accepted(self):
        ids = self.seed()
        inventory = self.s.items(10)
        one = {'outfits': [{'ids': [ids[0], ids[3], ids[4]]}]}
        two = {
            'outfits': [
                {'ids': [ids[0], ids[3], ids[4]]},
                {'ids': [ids[1], ids[3], ids[4]]},
            ]
        }
        self.assertEqual(len(validate_outfits(one, inventory)), 1)
        self.assertEqual(len(validate_outfits(two, inventory)), 2)

    def test_dress_and_shoes_can_start_wizard(self):
        self.s.add(10, 'dress', 'Платье / комбинезон', 'Платье', photo())
        self.s.add(10, 'shoes', 'Обувь', 'Обувь', photo())
        self.app.callback(10, 'looks')
        self.assertEqual(self.s.state(10)['step'], 0)

    def test_delete_and_erase_include_person_photo(self):
        ids = self.seed()
        outfit_id = self.s.save_outfit(10, {'ids': [ids[0], *ids[3:]]}, photo())
        self.s.delete(10, ids[0])
        with self.assertRaises(UserError):
            self.s.outfit(10, outfit_id)
        self.s.set_person_photo(10, photo())
        self.s.add(20, 'other', 'Верх', 'Other', photo())
        self.s.state(10, {'answers': {'style': 'x'}})
        self.s.erase(10)
        self.assertFalse(self.s.items(10))
        self.assertIsNone(self.s.person_photo(10))
        self.assertEqual(self.s.state(10), {})
        self.assertEqual(len(self.s.items(20)), 1)

    def test_invalid_ai_inventory_rejected(self):
        ids = self.seed()
        result = {'outfits': [{'ids': [ids[0], *ids[3:]]}] * 3}
        with self.assertRaises(UserError):
            validate_outfits(result, self.s.items(10))
        result['outfits'][0] = {'ids': [999, *ids[3:]]}
        with self.assertRaises(UserError):
            validate_outfits(result, self.s.items(10))

    def test_wrong_category_swap_rejected(self):
        ids = self.seed()
        outfit_id = self.s.save_outfit(10, {'ids': [ids[0], *ids[3:]]}, photo())
        with self.assertRaises(UserError):
            self.s.replacement(10, outfit_id, ids[0], ids[3])

    def test_photo_normalization(self):
        normalized = Image.open(io.BytesIO(normalize_photo(photo())))
        self.assertEqual(normalized.format, 'JPEG')
        self.assertLessEqual(max(normalized.size), 1280)

    def test_responses_payload_and_refusal(self):
        response = {
            'status': 'completed',
            'output': [{
                'content': [{
                    'type': 'output_text',
                    'text': json.dumps({
                        'valid': True,
                        'category': 'Верх',
                        'item_type': 'Футболка',
                        'description': 'Верх',
                    }),
                }]
            }],
        }
        with patch('bot.request', return_value=json.dumps(response).encode()) as call:
            AI('fake', 'gpt-5.1').classify(photo())
            payload = json.loads(call.call_args.args[1])
            self.assertFalse(payload['store'])
            self.assertTrue(payload['text']['format']['strict'])
            self.assertTrue(payload['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,'))
        with patch('bot.request', return_value=b'{"status":"completed","output":[]}'):
            with self.assertRaises(UserError):
                AI('fake', 'gpt-5.1').classify(photo())

    def test_outfit_photo_uses_one_multi_image_edit_request(self):
        encoded = base64.b64encode(photo()).decode()
        items = [
            {
                'category': category,
                'item_type': category,
                'description': category,
                'photo': photo(),
            }
            for category in ('Верх', 'Низ', 'Обувь')
        ]
        with patch(
            'bot.multipart_request',
            return_value=json.dumps({'data': [{'b64_json': encoded}]}).encode(),
        ) as call:
            AI('fake', 'gpt-5.1', 'gpt-image-2').outfit_photo(items)
            args = call.call_args.args
            self.assertEqual(args[0], 'https://api.openai.com/v1/images/edits')
            self.assertEqual(args[1]['model'], 'gpt-image-2')
            self.assertEqual(args[1]['size'], '1024x1536')
            self.assertEqual(args[1]['quality'], 'medium')
            self.assertEqual(args[1]['output_format'], 'jpeg')
            self.assertEqual(args[1]['output_compression'], '90')
            self.assertEqual(len(args[2]), 3)
            self.assertTrue(all(file[0] == 'image[]' for file in args[2]))
            self.assertIn('40–44%', args[1]['prompt'])
            self.assertIn('Reference 1', args[1]['prompt'])

    def test_try_on_sends_person_as_first_reference(self):
        encoded = base64.b64encode(photo()).decode()
        items = [{
            'category': 'Верх',
            'item_type': 'Футболка',
            'description': 'Белая',
            'photo': photo(),
        }]
        with patch(
            'bot.multipart_request',
            return_value=json.dumps({'data': [{'b64_json': encoded}]}).encode(),
        ) as call:
            AI('fake', 'gpt-5.1', 'gpt-image-2').try_on(photo(), items)
            files = call.call_args.args[2]
            self.assertEqual(files[0][1], 'person.jpg')
            self.assertEqual(files[1][0], 'image[]')
            self.assertIn('FIRST reference image', call.call_args.args[1]['prompt'])

    def test_prompts_define_proportions_and_identity_rules(self):
        self.assertIn('equal visual width', OUTFIT_IMAGE_PROMPT)
        self.assertIn('12–15%', OUTFIT_IMAGE_PROMPT)
        self.assertIn('Preserve that person', TRY_ON_PROMPT)
        self.assertIn('head to feet', TRY_ON_PROMPT)

    def test_visible_labels_do_not_expose_internal_item_ids(self):
        ids = self.seed()
        self.app.show_item(10, ids[0])
        self.app.listing(10, 0)
        outfit_id = self.s.save_outfit(
            10,
            {'title': 'Образ', 'reason': 'Причина', 'ids': [ids[0], ids[3], ids[4]], 'preferences': {}},
            photo(),
        )
        self.app.show_outfit(10, outfit_id)
        for _, _, text, buttons in self.t.sent:
            self.assertNotIn('#', text)
            for label, _ in buttons or []:
                self.assertNotIn('#', label)

    def test_legacy_outfit_without_image_is_generated_once(self):
        ids = self.seed()
        outfit_id = self.s.save_outfit(
            10,
            {'title': 'Старый образ', 'reason': 'Причина', 'ids': [ids[0], ids[3], ids[4]], 'preferences': {}},
            None,
        )
        self.app.show_outfit(10, outfit_id)
        self.app.show_outfit(10, outfit_id)
        self.assertEqual(self.ai.outfit_photo_calls, 1)
        self.assertIsNotNone(self.s.outfit(10, outfit_id)['photo'])

    def test_old_database_schema_migrates_without_losing_items(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            old = sqlite3.connect(path)
            old.executescript('''
              CREATE TABLE users (uid INTEGER PRIMARY KEY, consent INTEGER NOT NULL);
              CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
                source TEXT NOT NULL, category TEXT NOT NULL,
                description TEXT NOT NULL, photo BLOB NOT NULL,
                UNIQUE(uid, source));
              CREATE TABLE state (uid INTEGER PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE outfits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, data TEXT NOT NULL);
              CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            old.execute('INSERT INTO users VALUES (10,1)')
            old.execute(
                'INSERT INTO items(uid,source,category,description,photo) VALUES (?,?,?,?,?)',
                (10, 'old', 'Верх', 'Старая вещь', photo()),
            )
            old.commit()
            old.close()

            migrated = Store(path)
            try:
                tables = {
                    row[0]
                    for row in migrated.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                outfit_columns = {
                    row['name'] for row in migrated.db.execute('PRAGMA table_info(outfits)')
                }
                self.assertNotIn('users', tables)
                self.assertIn('profiles', tables)
                self.assertIn('photo', outfit_columns)
                self.assertIn('tryon_photo', outfit_columns)
                self.assertEqual(len(migrated.items(10)), 1)
                self.assertEqual(migrated.items(10)[0]['item_type'], 'Верх')
            finally:
                migrated.db.close()


class DispatcherTests(unittest.TestCase):
    def event(self, uid, number=0):
        return {
            'message': {
                'message_id': number,
                'chat': {'id': uid, 'type': 'private'},
                'from': {'id': uid},
                'text': '/start',
            }
        }

    def test_different_users_run_in_parallel(self):
        class ParallelApp:
            def __init__(self):
                self.lock = threading.Lock()
                self.started = set()
                self.both_started = threading.Event()
                self.release = threading.Event()

            def handle(self, update):
                uid = update['message']['from']['id']
                with self.lock:
                    self.started.add(uid)
                    if len(self.started) == 2:
                        self.both_started.set()
                self.release.wait(2)

            def report_error(self, update, exc):
                raise exc

        app = ParallelApp()
        dispatcher = UpdateDispatcher(app, workers=2)
        dispatcher.submit(self.event(1))
        dispatcher.submit(self.event(2))
        self.assertTrue(app.both_started.wait(1), 'different users did not start concurrently')
        app.release.set()
        dispatcher.shutdown()

    def test_same_user_updates_stay_sequential(self):
        class SerialApp:
            def __init__(self):
                self.lock = threading.Lock()
                self.active = 0
                self.maximum = 0
                self.order = []

            def handle(self, update):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                    self.order.append(update['message']['message_id'])
                time.sleep(0.02)
                with self.lock:
                    self.active -= 1

            def report_error(self, update, exc):
                raise exc

        app = SerialApp()
        dispatcher = UpdateDispatcher(app, workers=4)
        for number in range(4):
            dispatcher.submit(self.event(1, number))
        dispatcher.shutdown()
        self.assertEqual(app.maximum, 1)
        self.assertEqual(app.order, list(range(4)))


class PersistenceTests(unittest.TestCase):
    mountinfo = '''
21 1 0:1 / / rw,relatime - overlay overlay rw
22 21 0:2 / /data rw,relatime - ext4 /dev/vdb rw
'''

    def test_data_volume_is_detected(self):
        self.assertTrue(has_dedicated_mount(Path('/data'), self.mountinfo))
        self.assertTrue(has_dedicated_mount(Path('/data/nested'), self.mountinfo))

    def test_container_root_is_not_treated_as_persistent(self):
        root_only = '21 1 0:1 / / rw,relatime - overlay overlay rw\n'
        self.assertFalse(has_dedicated_mount(Path('/data'), root_only))

    def test_production_mode_rejects_missing_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict('os.environ', {'REQUIRE_PERSISTENT_DATA': '1'}, clear=False):
                with patch('bot.has_dedicated_mount', return_value=False):
                    with self.assertRaises(SystemExit) as caught:
                        require_persistent_storage(Path(directory))
        self.assertIn('не подключён как отдельный volume', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

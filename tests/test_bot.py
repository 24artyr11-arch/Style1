import io
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from core import Store, UserError, QUESTIONS, collage, normalize_photo, validate_outfits
from bot import App, AI
from unittest.mock import patch
import json

def photo():
    b = io.BytesIO()
    Image.new('RGB', (500, 600), '#789b85').save(b, 'JPEG')
    return b.getvalue()

class FakeTG:
    def __init__(self):
        self.sent = []
        self.calls = []
    def say(self, uid, text, buttons=None): self.sent.append(('text', uid, text, buttons))
    def photo(self, uid, raw, caption, buttons=None):
        Image.open(io.BytesIO(raw)).verify()
        self.sent.append(('photo', uid, caption, buttons))
    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True
    def download(self, file_id): return photo()

class FakeAI:
    calls = 0
    def classify(self, raw):
        self.calls += 1
        return {'valid': True, 'category': 'Верх', 'description': 'Зелёный верх'}
    def outfits(self, items, answers):
        tops = [i['id'] for i in items if i['category'] == 'Верх']
        bottom = next(i['id'] for i in items if i['category'] == 'Низ')
        shoes = next(i['id'] for i in items if i['category'] == 'Обувь')
        return validate_outfits({'outfits': [{'title': 'Комплект', 'reason': 'Пояснение', 'ids': [t, bottom, shoes]} for t in tops[:3]]}, items)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / 'db')
        self.t, self.ai = FakeTG(), FakeAI()
        self.app = App(self.s, self.t, self.ai)
        self.s.accept(10)

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def seed(self):
        return [self.s.add(10, str(n), cat, cat, photo()) for n, cat in enumerate(['Верх']*3 + ['Низ', 'Обувь'])]

    def event(self, uid, data=None, pics=None):
        msg = {'message_id': 123, 'chat': {'id': uid, 'type': 'private'}, 'from': {'id': uid}, 'text': '/start'}
        if pics: msg['photo'] = pics
        return {'callback_query': {'id': 'x', 'from': {'id': uid}, 'message': msg, 'data': data}} if data else {'message': msg}

    def test_full_wizard_and_swap(self):
        ids = self.seed()
        self.app.handle(self.event(10, 'looks'))
        for n in range(len(QUESTIONS)):
            nonce = self.s.state(10)['nonce']
            self.app.handle(self.event(10, f'q:{nonce}:{n}:0'))
        self.assertEqual(sum(x[0] == 'photo' for x in self.t.sent), 3)
        oids = self.s.state(10)['recent']
        second = self.s.outfit(10, oids[1])
        self.app.handle(self.event(10, f'pick:{oids[0]}:{ids[0]}:{ids[1]}'))
        self.assertIn(ids[1], self.s.outfit(10, oids[0])['ids'])
        self.assertEqual(second, self.s.outfit(10, oids[1]))

    def test_cross_user_access_denied(self):
        ids = self.seed()
        oid = self.s.save_outfit(10, {'ids': ids[:1]+ids[3:]})
        for operation in [lambda: self.s.item(20, ids[0]), lambda: self.s.delete(20, ids[0]),
                          lambda: self.s.outfit(20, oid), lambda: self.s.replace(20, oid, ids[0], ids[1])]:
            with self.assertRaises(UserError): operation()

    def test_photo_consent_for_any_user(self):
        p = [{'file_id': 'x', 'file_unique_id': 'x'}]
        self.app.handle(self.event(99, pics=p))
        self.assertEqual(self.ai.calls, 0)
        self.assertFalse(self.s.items(99))
        self.app.handle(self.event(99, 'consent'))
        self.app.handle(self.event(99, pics=p))
        self.assertEqual(self.ai.calls, 1)
        self.assertEqual(len(self.s.items(99)), 1)

    def test_album_photos_independent_and_duplicate_free(self):
        for n in ('a', 'b', 'a'):
            self.app.handle(self.event(10, pics=[{'file_id': n, 'file_unique_id': n}]))
        self.assertEqual(len(self.s.items(10)), 2)
        self.assertEqual(self.ai.calls, 2)

    def test_stale_wizard_cannot_trigger_paid_call(self):
        self.seed()
        self.app.callback(10, 'looks')
        nonce = self.s.state(10)['nonce']
        self.app.callback(10, f'q:{nonce}:0:0')
        with self.assertRaises(UserError): self.app.callback(10, f'q:{nonce}:0:0')

    def test_callback_deletes_previous_message(self):
        self.app.handle(self.event(10, 'menu'))
        self.assertTrue(any(args and args[0] == 'deleteMessage' and
                            kwargs == {} and args[1] == {'chat_id': 10, 'message_id': 123}
                            for args, kwargs in self.t.calls))

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
        two = {'outfits': [
            {'ids': [ids[0], ids[3], ids[4]]},
            {'ids': [ids[1], ids[3], ids[4]]},
        ]}
        self.assertEqual(len(validate_outfits(one, inventory)), 1)
        self.assertEqual(len(validate_outfits(two, inventory)), 2)

    def test_dress_and_shoes_can_start_wizard(self):
        self.s.add(10, 'dress', 'Платье / комбинезон', 'Платье', photo())
        self.s.add(10, 'shoes', 'Обувь', 'Обувь', photo())
        self.app.callback(10, 'looks')
        self.assertEqual(self.s.state(10)['step'], 0)

    def test_delete_and_erase(self):
        ids = self.seed()
        oid = self.s.save_outfit(10, {'ids': [ids[0], *ids[3:]]})
        self.s.delete(10, ids[0])
        with self.assertRaises(UserError): self.s.outfit(10, oid)
        self.s.add(20, 'other', 'Верх', 'Other', photo())
        self.s.state(10, {'answers': {'style': 'x'}})
        self.s.erase(10)
        self.assertFalse(self.s.items(10))
        self.assertFalse(self.s.consent(10))
        self.assertEqual(self.s.state(10), {})
        self.assertEqual(len(self.s.items(20)), 1)

    def test_invalid_ai_inventory_rejected(self):
        ids = self.seed()
        result = {'outfits': [{'ids': [ids[0], *ids[3:]]}]*3}
        with self.assertRaises(UserError): validate_outfits(result, self.s.items(10))
        result['outfits'][0] = {'ids': [999, *ids[3:]]}
        with self.assertRaises(UserError): validate_outfits(result, self.s.items(10))

    def test_wrong_category_swap_rejected(self):
        ids = self.seed()
        oid = self.s.save_outfit(10, {'ids': [ids[0], *ids[3:]]})
        with self.assertRaises(UserError): self.s.replace(10, oid, ids[0], ids[3])

    def test_collage_and_normalization(self):
        ids = self.seed()
        im = Image.open(io.BytesIO(collage([self.s.item(10, i) for i in ids])))
        self.assertEqual(im.size, (1000, 1648))
        self.assertEqual(Image.open(io.BytesIO(normalize_photo(photo()))).format, 'JPEG')

    def test_responses_payload_and_refusal(self):
        response = {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'valid': True, 'category': 'Верх', 'description': 'Верх'})}]}]}
        with patch('bot.request', return_value=json.dumps(response).encode()) as call:
            AI('fake', 'gpt-5.1').classify(photo())
            payload = json.loads(call.call_args.args[1])
            self.assertFalse(payload['store'])
            self.assertTrue(payload['text']['format']['strict'])
            self.assertTrue(payload['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,'))
        with patch('bot.request', return_value=b'{"status":"completed","output":[]}'):
            with self.assertRaises(UserError): AI('fake', 'gpt-5.1').classify(photo())

if __name__ == '__main__': unittest.main()

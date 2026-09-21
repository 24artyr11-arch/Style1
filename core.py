"""Persistent wardrobe storage and image normalization. No network calls here."""
import io
import json
import sqlite3
import threading

from PIL import Image, ImageOps


CATEGORIES = ['Верх', 'Низ', 'Платье / комбинезон', 'Верхняя одежда', 'Обувь', 'Сумка', 'Аксессуары']
QUESTIONS = [
    ('temperature', 'Какая температура?', ['Ниже 0 °C', '0–10 °C', '10–20 °C', '20–25 °C', 'Выше 25 °C']),
    ('weather', 'Какая погода?', ['Сухо', 'Дождь', 'Снег', 'Ветрено']),
    ('occasion', 'Куда собираешься?', ['На каждый день', 'На работу', 'На прогулку', 'На свидание', 'На праздник']),
    ('style', 'Какой стиль?', ['На усмотрение стилиста', 'Повседневный', 'Деловой', 'Спортивный', 'Минимализм']),
    ('colors', 'Какие цвета предпочитаешь?', ['Любые', 'Нейтральные', 'Яркие акценты', 'Монохром']),
]


class UserError(Exception):
    pass


class Store:
    """One serialized SQLite connection shared by parallel user workers."""

    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute('PRAGMA busy_timeout=5000')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA secure_delete=ON')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
                source TEXT NOT NULL, category TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL, photo BLOB NOT NULL, display_photo BLOB,
                UNIQUE(uid, source));
              CREATE TABLE IF NOT EXISTS profiles (
                uid INTEGER PRIMARY KEY, person_photo BLOB);
              CREATE TABLE IF NOT EXISTS state (uid INTEGER PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS outfits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, data TEXT NOT NULL,
                photo BLOB, tryon_photo BLOB);
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            item_columns = {r['name'] for r in self.db.execute('PRAGMA table_info(items)')}
            if 'item_type' not in item_columns:
                self.db.execute("ALTER TABLE items ADD COLUMN item_type TEXT NOT NULL DEFAULT ''")
            if 'display_photo' not in item_columns:
                self.db.execute('ALTER TABLE items ADD COLUMN display_photo BLOB')
            outfit_columns = {r['name'] for r in self.db.execute('PRAGMA table_info(outfits)')}
            if 'photo' not in outfit_columns:
                self.db.execute('ALTER TABLE outfits ADD COLUMN photo BLOB')
            if 'tryon_photo' not in outfit_columns:
                self.db.execute('ALTER TABLE outfits ADD COLUMN tryon_photo BLOB')
            self.db.execute("UPDATE items SET item_type=category WHERE item_type='' OR item_type IS NULL")
            # The old table only stored the removed consent gate.
            self.db.execute('DROP TABLE IF EXISTS users')
            self.db.commit()

    def items(self, uid):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                'SELECT id,category,item_type,description FROM items WHERE uid=? ORDER BY id', (uid,))]

    def item(self, uid, iid):
        with self.lock:
            row = self.db.execute('SELECT * FROM items WHERE uid=? AND id=?', (uid, iid)).fetchone()
            if not row:
                raise UserError('Вещь уже удалена или недоступна.')
            return dict(row)

    def duplicate(self, uid, source):
        with self.lock:
            return self.db.execute('SELECT id FROM items WHERE uid=? AND source=?', (uid, source)).fetchone()

    def add(self, uid, source, category, description, photo, item_type=''):
        if category not in CATEGORIES:
            raise UserError('Не удалось определить категорию. Попробуй другое фото.')
        item_type = (item_type or category)[:80]
        with self.lock, self.db:
            return self.db.execute(
                'INSERT INTO items(uid,source,category,item_type,description,photo,display_photo) '
                'VALUES (?,?,?,?,?,?,NULL)',
                (uid, source, category, item_type, description[:600], photo)
            ).lastrowid

    def change_category(self, uid, iid, category):
        self.item(uid, iid)
        with self.lock, self.db:
            self.db.execute('UPDATE items SET category=? WHERE uid=? AND id=?', (category, uid, iid))
            self.db.execute('DELETE FROM outfits WHERE uid=?', (uid,))

    def delete(self, uid, iid):
        self.item(uid, iid)
        with self.lock, self.db:
            self.db.execute('DELETE FROM items WHERE uid=? AND id=?', (uid, iid))
            self.db.execute('DELETE FROM outfits WHERE uid=?', (uid,))

    def person_photo(self, uid):
        with self.lock:
            row = self.db.execute('SELECT person_photo FROM profiles WHERE uid=?', (uid,)).fetchone()
            return row['person_photo'] if row and row['person_photo'] else None

    def set_person_photo(self, uid, photo):
        with self.lock, self.db:
            self.db.execute(
                'INSERT INTO profiles(uid,person_photo) VALUES (?,?) '
                'ON CONFLICT(uid) DO UPDATE SET person_photo=excluded.person_photo',
                (uid, photo),
            )
            self.db.execute('UPDATE outfits SET tryon_photo=NULL WHERE uid=?', (uid,))

    def delete_person_photo(self, uid):
        with self.lock, self.db:
            self.db.execute('DELETE FROM profiles WHERE uid=?', (uid,))
            self.db.execute('UPDATE outfits SET tryon_photo=NULL WHERE uid=?', (uid,))

    def erase(self, uid):
        with self.lock:
            with self.db:
                for table in ('items', 'outfits', 'state', 'profiles'):
                    self.db.execute(f'DELETE FROM {table} WHERE uid=?', (uid,))
            self.db.execute('VACUUM')

    def state(self, uid, value=None):
        with self.lock:
            if value is not None:
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (uid, json.dumps(value)))
                return value
            row = self.db.execute('SELECT data FROM state WHERE uid=?', (uid,)).fetchone()
            return json.loads(row[0]) if row else {}

    @staticmethod
    def _outfit_data(data):
        return {k: v for k, v in data.items() if k not in ('photo', 'tryon_photo')}

    def save_outfit(self, uid, data, photo):
        with self.lock, self.db:
            oid = self.db.execute(
                'INSERT INTO outfits(uid,data,photo) VALUES (?,?,?)',
                (uid, json.dumps(self._outfit_data(data)), photo),
            ).lastrowid
            self.db.execute(
                'DELETE FROM outfits WHERE uid=? AND id NOT IN '
                '(SELECT id FROM outfits WHERE uid=? ORDER BY id DESC LIMIT 30)',
                (uid, uid),
            )
            return oid

    def outfit(self, uid, oid):
        with self.lock:
            row = self.db.execute(
                'SELECT data,photo,tryon_photo FROM outfits WHERE uid=? AND id=?', (uid, oid)
            ).fetchone()
            if not row:
                raise UserError('Этот образ устарел. Нажми «Подобрать образы».')
            data = json.loads(row['data'])
            for iid in data['ids']:
                self.item(uid, iid)
            data['photo'] = row['photo']
            data['tryon_photo'] = row['tryon_photo']
            return data

    def recent_outfit_ids(self, uid, limit=3):
        with self.lock:
            return [r['id'] for r in self.db.execute(
                'SELECT id FROM outfits WHERE uid=? ORDER BY id DESC LIMIT ?', (uid, limit)
            )]

    def set_outfit_photo(self, uid, oid, photo):
        self.outfit(uid, oid)
        with self.lock, self.db:
            self.db.execute('UPDATE outfits SET photo=? WHERE uid=? AND id=?', (photo, uid, oid))

    def set_tryon_photo(self, uid, oid, photo):
        self.outfit(uid, oid)
        with self.lock, self.db:
            self.db.execute('UPDATE outfits SET tryon_photo=? WHERE uid=? AND id=?', (photo, uid, oid))

    def replacement(self, uid, oid, old, new):
        data = self.outfit(uid, oid)
        before, after = self.item(uid, old), self.item(uid, new)
        if old not in data['ids'] or new in data['ids'] or before['category'] != after['category']:
            raise UserError('Замена недоступна. Выбери другую вещь.')
        data['ids'] = [new if iid == old else iid for iid in data['ids']]
        data['reason'] = 'Комплект с твоей заменой.'
        return self._outfit_data(data)

    def update_outfit(self, uid, oid, data, photo):
        self.outfit(uid, oid)
        with self.lock, self.db:
            self.db.execute(
                'UPDATE outfits SET data=?,photo=?,tryon_photo=NULL WHERE uid=? AND id=?',
                (json.dumps(self._outfit_data(data)), photo, uid, oid),
            )

    def offset(self, value=None):
        with self.lock:
            if value is not None:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (str(value),))
            row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            return int(row[0]) if row else 0


def normalize_photo(raw):
    if len(raw) > 12 * 1024 * 1024:
        raise UserError('Фото слишком большое. Отправь его как фото, а не файл.')
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 25_000_000:
            raise UserError('Уменьши разрешение фотографии.')
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((1280, 1280))
        result = io.BytesIO()
        image.save(result, 'JPEG', quality=90)
        return result.getvalue()


def validate_outfits(result, inventory):
    outfits = result.get('outfits', [])
    if not outfits:
        raise UserError(result.get('message', 'Недостаточно вещей для подходящего образа.')[:800])
    if not 1 <= len(outfits) <= 3:
        raise UserError('ИИ вернул некорректное количество образов. Попробуй ещё раз.')
    known = {item['id']: item for item in inventory}
    seen = set()
    for outfit in outfits:
        ids = outfit['ids']
        if not 2 <= len(ids) <= 8 or len(ids) != len(set(ids)) or any(iid not in known for iid in ids):
            raise UserError('ИИ предложил некорректный комплект. Попробуй ещё раз.')
        categories = {known[iid]['category'] for iid in ids}
        if not ('Обувь' in categories and ('Платье / комбинезон' in categories or {'Верх', 'Низ'} <= categories)):
            raise UserError('Для полного образа нужны обувь и верх с низом либо платье / комбинезон.')
        key = tuple(sorted(ids))
        if key in seen:
            raise UserError('Образы должны отличаться набором вещей. Попробуй ещё раз.')
        seen.add(key)
    return outfits

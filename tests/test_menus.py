import copy
import time
import unittest
from unittest.mock import patch
import test_queue as fixtures
from app import Store, Worker
from core import GB
from net import APIError, Telegram
from test_seasons import files


class Menus(fixtures.Lifecycle):
    def buttons(self):
        return [b for row in self.tg.markups[-1]['inline_keyboard'] for b in row]

    def click(self, label=None, data=None, uid=123, chat=None):
        if data is None:
            matches = [b for b in self.buttons() if label == b['text']] or [b for b in self.buttons() if label in b['text']]
            self.assertEqual(len(matches), 1, (label, self.buttons()))
            data = matches[0]['callback_data']
        self.w.callback({'id': 'test-query', 'from': {'id': uid}, 'data': data,
                         'message': {'message_id': 42, 'chat': {'id': uid if chat is None else chat, 'type': 'private'}}})
        return data

    def test_files_select_by_name_without_hash(self):
        self.w.tick(); self.command('/files')
        self.assertIn('Выбери раздачу', self.tg.messages[-1])
        self.assertNotIn(fixtures.H[:8], str(self.tg.markups[-1]))
        self.click('Show')
        self.assertIn('S01E01', self.tg.messages[-1])
        self.assertIn('ожидает места', self.tg.messages[-1])
        self.assertTrue(self.tg.edits)
        self.assertEqual(self.tg.answers[-1], ('test-query', '', False))

    def test_multiseason_combined_selection_starts_without_resume(self):
        self.q.files[fixtures.H] = files('Show.S01E01.mkv', 'Show.S02E01-E02.mkv', 'Show.S02E03.mkv')
        self.w.tick(); self.command('/from'); self.click('Show'); self.click('Сезон 2'); self.click('Серии 1–2')
        self.assertEqual((self.s['season'], self.s['start_episode']), (2, 1))
        self.assertFalse(self.store.data['paused'])
        self.assertTrue(self.s['episodes'][0]['skipped_start'])
        self.w.tick()
        self.assertTrue(self.s['episodes'][1]['selected'])

    def test_custom_start_inside_combined_episode(self):
        self.q.files[fixtures.H] = files('Show.S02E01-E02.mkv', 'Show.S02E03.mkv')
        self.w.tick(); self.command('/from'); self.click('Show'); self.click('Ввести сезон'); self.command('2 2')
        self.assertEqual(self.s['start_episode'], 2)
        self.assertFalse(self.s['episodes'][0]['skipped_start'])

    def test_explicit_pause_survives_add_and_start_change(self):
        self.command('/pause')
        response = self.w.add('New', 2, 'magnet:?xt=urn:btih:' + 'b' * 40)
        self.assertTrue(self.store.data['paused']); self.assertIn('паузу', response)
        self.command('/from ' + fixtures.H[:8] + ' 1 3')
        self.assertTrue(self.store.data['paused'])
        self.command('/menu'); self.click('Продолжить')
        self.assertFalse(self.store.data['paused']); self.assertNotIn('pause_reason', self.store.data)

    def test_legacy_automatic_pause_cleared_on_add(self):
        self.store.data['paused'] = True
        response = self.w.add('New', 2, 'magnet:?xt=urn:btih:' + 'b' * 40)
        self.assertFalse(self.store.data['paused']); self.assertIn('автоматически', response)
        self.assertNotIn('bbbbbbbb', response); self.assertNotIn(fixtures.H[:8], self.w.status())

    def test_settings_presets_and_custom_comma(self):
        self.command('/limit'); self.click('По числу серий и гигабайтам'); self.click('5 10')
        self.assertEqual((self.store.data['count'], self.store.data['cap']), (5, 10 * GB))
        self.command('/limit'); self.click('По гигабайтам'); self.command('7,5')
        self.assertEqual((self.store.data['count'], self.store.data['cap']), (None, int(7.5 * GB)))
        self.command('/notify'); self.click('0'); self.assertEqual(self.store.data['notify'], 0)
        self.command('/reserve'); self.command('2,5'); self.assertEqual(self.store.data['reserve'], int(2.5 * GB))

    def test_invalid_value_retry_and_cancel(self):
        self.command('/limit'); self.click('По числу серий')
        with self.assertRaises(ValueError): self.command('0')
        self.assertIn('123', self.store.data['ui_input'])
        self.command('8'); self.assertEqual(self.store.data['count'], 8)
        self.command('/reserve'); self.command('/cancel'); self.assertNotIn('123', self.store.data['ui_input'])

    def test_family_inputs_independent_survive_restart(self):
        self.store.data['members'] = {'456': {'name': 'Family'}}
        self.command('/notify'); self.command('/reserve', sender=456)
        fresh = Store(self.store.path)
        self.w = Worker(fresh, self.q, self.j, self.tg, self.root / 'data')
        self.command('4'); self.command('3', sender=456)
        self.assertEqual((fresh.data['notify'], fresh.data['reserve']), (4, 3 * GB))
        self.assertFalse(fresh.data['ui_input'])

    def test_callback_user_chat_expiry_scoping(self):
        self.store.data['members'] = {'456': {'name': 'Family'}}
        self.command('/menu'); data = next(b['callback_data'] for b in self.buttons() if 'Пауза' in b['text'])
        self.click(data=data, uid=456); self.assertFalse(self.store.data['paused'])
        self.click(data=data, chat=456); self.assertFalse(self.store.data['paused'])
        self.store.data['buttons'][data[3:]]['expires'] = time.time() - 1
        self.click(data=data); self.assertFalse(self.store.data['paused']); self.assertTrue(self.tg.answers[-1][2])

    def test_old_button_cannot_modify_replacement(self):
        self.w.tick(); self.command('/finish'); data = self.buttons()[0]['callback_data']
        self.q.infos.clear(); self.w.add('New name', 2, fixtures.MAGNET)
        new = self.store.data['seasons'][0]; self.click(data=data)
        self.assertFalse(new['notified']); self.assertTrue(self.tg.answers[-1][2])

    def test_typed_start_cannot_modify_replacement(self):
        self.w.tick(); self.command('/from'); self.click('Show'); self.click('Ввести сезон')
        self.q.infos.clear(); self.w.add('New name', 2, fixtures.MAGNET)
        with self.assertRaises(ValueError): self.command('1 3')
        self.assertEqual(self.store.data['seasons'][0]['season'], 2)

    def test_buttons_survive_restart(self):
        self.command('/files'); data = self.buttons()[0]['callback_data']
        self.w = Worker(Store(self.store.path), self.q, self.j, self.tg, self.root / 'data')
        self.click(data=data); self.assertIn('Show', self.tg.messages[-1]); self.assertFalse(self.tg.answers[-1][2])

    def test_restore_missing_without_magnet_reentry(self):
        self.w.tick(); self.q.infos.clear(); self.w.tick()
        self.command('/files'); self.click('Show'); self.click('Восстановить')
        new = self.store.data['seasons'][0]
        self.assertEqual(new['phase'], 'metadata'); self.assertFalse(new['submitted'])
        self.w.tick(); self.assertEqual(len([x for x in self.q.calls if x[0] == 'add']), 1)
        self.w.tick(); self.assertEqual(len([x for x in self.q.calls if x[0] == 'add']), 1)

    def test_finish_and_next_season_menu(self):
        self.w.tick(); self.command('/finish'); self.click('Show'); self.assertTrue(self.s['notified'])
        self.command('/files'); self.click('Show'); self.click('Добавить продолжение')
        d = self.store.data['dialogs']['123']
        self.assertEqual((d['title'], d['season'], d['step']), ('Show', 2, 'magnet'))
        self.command('magnet:?xt=urn:btih:' + 'b' * 40)
        self.assertEqual(self.store.data['seasons'][1]['season'], 2)

    def test_owner_revoke_by_name_disables_old_buttons(self):
        self.store.data['members'] = {'456': {'name': 'Family'}}
        self.command('/menu', sender=456); old = self.buttons()[0]['callback_data']
        self.command('/users'); self.click('Family'); confirm = self.buttons()[0]['callback_data']
        self.click(data=confirm, uid=456); self.assertIn(456, self.w.users)
        self.click(data=confirm); self.assertNotIn(456, self.w.users)
        self.assertNotIn(old[3:], self.store.data['buttons'])
        self.click(data=old, uid=456); self.assertNotIn('456', self.store.data.get('dialogs', {}))

    def test_movies_have_no_season_actions(self):
        self.s['kind'] = 'movie'; self.command('/files'); self.click('Show')
        self.assertFalse(any('С какой серии' in b['text'] for b in self.buttons()))
        self.command('/from'); self.assertFalse(any('Show' in b['text'] for b in self.buttons()))

    def test_pagination(self):
        self.q.files[fixtures.H] = [fixtures.file(i) for i in range(40)]; self.w.tick()
        for n in range(9):
            p = copy.deepcopy(self.s); p['hash'] = str(n) * 40; p['title'] = f'Other {n}'
            self.store.data['seasons'].append(p)
        self.command('/files'); self.click('Дальше')
        self.assertTrue(any('Other 8' in b['text'] for b in self.buttons()))
        self.click('Назад'); self.click('Show'); self.click('Файлы →')
        self.assertIn('S01E09', self.tg.messages[-1]); self.assertNotIn('S01E01', self.tg.messages[-1])
        self.assertLess(len(self.tg.messages[-1]), 4000)

    def test_notifications_have_short_scoped_buttons(self):
        self.store.data['members'] = {'456': {'name': 'Family'}}
        self.w.broadcast('Next season', reply_target=fixtures.H)
        self.assertTrue(any('Добавить продолжение' in b['text'] for b in self.buttons()))
        for token, item in self.store.data['buttons'].items():
            self.assertLessEqual(len(('ui:' + token).encode()), 64); self.assertIn(item['uid'], (123, 456))
        self.assertNotIn(fixtures.H[:8], str(self.tg.markups[-1]))

    def test_old_callback_ack_does_not_interrupt_valid_action(self):
        self.command('/menu')
        with patch.object(self.tg, 'answer', side_effect=APIError('HTTP 400')): self.click('Пауза')
        self.assertTrue(self.store.data['paused'])

    def test_deleted_message_falls_back_to_new_message(self):
        self.command('/menu')
        with patch.object(self.tg, 'edit', side_effect=APIError('HTTP 400')): self.click('Настройки')
        self.assertIn('Лимит', self.tg.sent[-1][1])

    def test_new_input_does_not_feed_old_add_dialog(self):
        self.command('/add'); self.command('Сериал'); self.command('/limit'); self.click('По числу серий'); self.command('8')
        self.assertEqual(self.store.data['count'], 8); self.assertNotIn('123', self.store.data['dialogs'])


for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in Menus.__dict__: setattr(Menus, name, None)


class TelegramMenus(unittest.TestCase):
    def test_polling_and_callback_transport(self):
        t = Telegram.__new__(Telegram)
        with patch.object(t, 'call', return_value=[]) as call:
            t.updates(12); self.assertIn('callback_query', call.call_args.kwargs['allowed_updates'])
            t.answer('qid', 'Done', True); self.assertEqual(call.call_args.kwargs['show_alert'], 'true')
            t.edit('Files', 123, 4, {'inline_keyboard': []})
            self.assertEqual(call.call_args.args, ('editMessageText',))
            self.assertEqual(call.call_args.kwargs['message_id'], 4)


if __name__ == '__main__': unittest.main()

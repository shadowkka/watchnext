import time
import unittest
from unittest.mock import patch
from net import APIError
import test_queue as fixtures


class Family(fixtures.Lifecycle):
    # Reuse fixture only; original lifecycle tests remain in their own class.
    def invite(self):
        self.command('/invite')
        link = self.tg.messages[-1]
        return link.split('start=join_', 1)[1].split('\n', 1)[0]

    def join(self, token, uid=456):
        self.w.command({'from': {'id': uid, 'first_name': 'Family'},
                        'chat': {'id': uid, 'type': 'private'}, 'text': '/start join_' + token})

    def test_invitation_one_use_and_membership_persisted(self):
        token = self.invite()
        self.assertNotIn(token, self.store.path.read_text())
        self.join(token)
        self.assertIn(456, self.w.users)
        self.assertIsNone(self.store.data.get('invite'))
        self.command('/limit episodes 2', sender=456)
        self.assertEqual(self.store.data['count'], 2)
        self.assertEqual(self.tg.sent[-1][0], 456)
        with self.assertRaises(ValueError): self.join(token, 789)
        self.assertNotIn(789, self.w.users)
        from app import Store
        self.assertIn('456', Store(self.store.path).data['members'])

    def test_expired_and_invalid_invites(self):
        token = self.invite()
        with self.assertRaises(ValueError): self.join(token + 'x')
        self.store.data['invite']['expires'] = time.time() - 1
        with self.assertRaises(ValueError): self.join(token)
        self.assertNotIn(456, self.w.users)

    def test_new_invitation_invalidates_previous(self):
        first = self.invite(); second = self.invite()
        with self.assertRaises(ValueError): self.join(first)
        self.join(second)
        self.assertIn(456, self.w.users)

    def test_members_cannot_invite_or_revoke(self):
        self.join(self.invite())
        for cmd in ('/invite', '/users', '/revoke 123'):
            with self.assertRaises(ValueError): self.command(cmd, sender=456)
        self.assertIn(123, self.w.users)
        with self.assertRaises(ValueError): self.command('/revoke 123')

    def test_revoke_blocks_further_commands(self):
        self.join(self.invite())
        self.command('/revoke 456')
        self.assertNotIn(456, self.w.users)
        self.command('/limit episodes 2', sender=456)
        self.assertEqual(self.store.data['count'], 5)
        self.assertNotIn('456', self.store.data['members'])

    def test_group_join_denied(self):
        token = self.invite()
        self.command('/start join_' + token, sender=456, chat_type='group')
        self.assertNotIn(456, self.w.users)
        self.assertIn('invite', self.store.data)

    def test_notifications_to_both_responses_only_to_sender(self):
        self.join(self.invite())
        self.tg.sent.clear(); self.tg.messages.clear()
        self.w.alert('test', 'Family notification')
        self.assertEqual({uid for uid, _ in self.tg.sent}, {123, 456})
        self.tg.sent.clear(); self.command('/status', sender=456)
        self.assertEqual([uid for uid, _ in self.tg.sent], [456])

    def test_message_ids_are_scoped_to_chats(self):
        self.join(self.invite())
        self.w.tick()
        for e in self.s['episodes'][:4]: e['watched'], e['removed'], e['selected'] = True, True, False
        self.tg.sent.clear(); self.tg.messages.clear()
        self.w.tick()
        replies=self.store.data['replies']
        self.assertIn('123:1', replies)
        self.assertIn('456:1', replies)
        self.assertEqual(replies['123:1'], replies['456:1'])
        self.command('magnet:?xt=urn:btih:' + 'b' * 40, sender=456, reply=1)
        self.assertEqual(self.store.data['seasons'][1]['season'], 2)
        self.assertEqual(self.tg.sent[-1][0], 456)

    def test_legacy_owner_reply_still_works(self):
        self.store.data['replies']['7'] = self.s['hash']
        self.command('magnet:?xt=urn:btih:' + 'b' * 40, reply=7)
        self.assertEqual(len(self.store.data['seasons']),2)

    def test_cross_chat_reply_cannot_pick_different_season(self):
        self.join(self.invite())
        self.store.data['replies']['123:7'] = self.s['hash']
        with self.assertRaises(ValueError):
            self.command('magnet:?xt=urn:btih:' + 'b' * 40, sender=456, reply=7)
        self.assertEqual(len(self.store.data['seasons']),1)

    def test_error_reply_goes_to_sender(self):
        self.join(self.invite()); self.tg.sent.clear()
        self.w.reply_error({'from': {'id':456},'chat': {'id':456,'type':'private'}}, ValueError('invalid limit'))
        self.assertEqual(self.tg.sent,[(456,'invalid limit')])

    def test_partial_notification_failure_keeps_reply_and_retries_only_missing_chat(self):
        self.join(self.invite())
        original = self.tg.send
        def send(text, chat_id=None, reply_markup=None):
            if chat_id == 456: raise APIError('timeout')
            return original(text, chat_id=chat_id)
        delivered = self.s.setdefault('notification_delivered', {})
        self.tg.sent.clear()
        with patch.object(self.tg, 'send', side_effect=send):
            with self.assertRaises(APIError):
                self.w.broadcast('next season', delivered, reply_target=self.s['hash'])
        self.assertEqual(self.store.data['replies']['123:1'], self.s['hash'])
        self.w.broadcast('next season', delivered, reply_target=self.s['hash'])
        self.assertEqual([uid for uid, _ in self.tg.sent], [123, 456])
        self.assertEqual(self.store.data['replies']['456:1'], self.s['hash'])

    def test_blocked_member_does_not_stop_other_notifications(self):
        self.join(self.invite())
        original=self.tg.send
        def send(text,chat_id=None,reply_markup=None):
            if chat_id==456: raise APIError('HTTP 403')
            return original(text,chat_id=chat_id)
        with patch.object(self.tg,'send',side_effect=send):
            result=self.w.broadcast('message')
        self.assertIsInstance(result['123'], int)
        self.assertIsNone(result['456'])

# Do not run Lifecycle's tests twice merely because we reuse its setup helpers.
for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in Family.__dict__:
        setattr(Family, name, None)

if __name__ == '__main__': unittest.main()

import copy
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

import test_queue as fixtures
from app import Store,Worker
from net import APIError
from subscriptions import TVmaze,scan_subscription,release_matches,release_is_show
from test_search import row


def sub():
    return {'show':{'id':42,'name':'Show','premiered':'2025-01-01'},'title':'Show','query':'Show',
            'since':time.time()-3600,'enabled':True,'activated':True,'events':{},'dismissed':[],
            'quality':'1080p','voice':'LostFilm','generation':'g','revision':0,'next_check':time.time()+7200}


def event(choices=None):
    return {'season':2,'number':1,'name':'Premiere','air':time.time()-100,'calendar':{},'ready':{},'choices':choices or []}


class Metadata(unittest.TestCase):
    def test_episode_dates_future_unknown_and_specials(self):
        rows=[{'season':2,'number':1,'name':'New','airstamp':'2026-10-21T12:00:00+00:00'},
              {'season':2,'number':2,'airdate':'2026-10-22'}, {'season':2,'number':3,'airdate':None},
              {'season':0,'number':1,'airdate':'2026-10-22'}]
        with patch.object(TVmaze,'get',return_value=rows):x=TVmaze().episodes(42)
        self.assertEqual([(e['season'],e['number']) for e in x],[(2,1),(2,2)])
        self.assertTrue(x[1]['air']>x[0]['air'])

    def test_release_ranges_and_season_packs_do_not_claim_wrong_episode(self):
        self.assertTrue(release_matches('Show S02E03-E04',2,4))
        self.assertTrue(release_matches('Show Сезон 2 Серии 1-5 из 10',2,5))
        self.assertFalse(release_matches('Show S02E01-03',2,4))
        self.assertFalse(release_matches('Show S03E04',2,4))
        self.assertTrue(release_matches('Show S02',2,1))
        self.assertFalse(release_matches('Show S02',2,5))
        self.assertFalse(release_matches('Some other movie 2026 1080p',2,1))
        self.assertTrue(release_is_show('Перевод / Show S02E01',sub()))
        self.assertFalse(release_is_show('Other Show S02E01',sub()))

    def test_scan_only_after_subscription_and_quality_voice_matching(self):
        s=sub();old=event();old['air']=s['since']-1;old['number']=9
        e=event();future=event();future['number']=2;future['air']=time.time()+1000
        good=row();good['title']='Show S02E01 1080p LostFilm';good['seeders']=5
        bad=copy.deepcopy(good);bad['title']='Show S02E01 720p LostFilm'
        with patch.object(TVmaze,'episodes',return_value=[old,e,future]),patch('subscriptions.Jackett.search',return_value={'results':[bad,good],'warnings':[]}):
            x=scan_subscription(s)
        self.assertEqual(list(x['events']),['2:1']);self.assertEqual(x['events']['2:1']['choices'],[good])
        self.assertEqual(x['next_episode']['number'],2)

    def test_tracker_failure_keeps_calendar_event(self):
        with patch.object(TVmaze,'episodes',return_value=[event()]),patch('subscriptions.Jackett.search',side_effect=APIError('down')):
            x=scan_subscription(sub())
        self.assertIn('2:1',x['events']);self.assertEqual(x['events']['2:1']['choices'],[]);self.assertTrue(x['warning'])

    def test_unknown_seeders_or_empty_swarm_not_offered(self):
        r=row();r['title']='Show S02E01 1080p LostFilm';r['seeders']=0
        with patch.object(TVmaze,'episodes',return_value=[event()]),patch('subscriptions.Jackett.search',return_value={'results':[r],'warnings':[]}):
            self.assertEqual(scan_subscription(sub())['events']['2:1']['choices'],[])


class Subscriptions(fixtures.Lifecycle):
    def setUp(self):
        super().setUp();self.futures=[]
        def submit(key,kind,fn,*args):
            f=Future();self.w.follow_jobs()[(key,kind)]=f;self.futures.append(f)
        self.patcher=patch.object(self.w,'follow_submit',side_effect=submit);self.patcher.start()
    def tearDown(self):self.patcher.stop();super().tearDown()
    def buttons(self):return [b for row in self.tg.markups[-1]['inline_keyboard'] for b in row]
    def click(self,label,uid=123):
        found=[b for b in self.buttons() if b['text']==label] or [b for b in self.buttons() if label in b['text']]
        self.assertEqual(len(found),1,(label,self.buttons()))
        self.w.callback({'id':'f','from':{'id':uid},'data':found[0]['callback_data'],'message':{'message_id':42,'chat':{'id':uid,'type':'private'}}})
    def install(self,s=None):
        s=s or sub();self.w.follow_all()['42']=s;return s
    def offer(self):
        r=row();r['title']='Show S02E01 1080p LostFilm';return r

    def test_interactive_confirmation_settings_and_activation(self):
        self.command('/follow');self.command('Show')
        self.assertEqual(self.w.follow_all(),{})
        self.futures[-1].set_result([{'id':42,'name':'Show','premiered':'2025-01-01'}]);self.w.follow_cycle()
        self.click('Show');self.assertFalse(self.w.follow_all()['42']['enabled'])
        self.click('Выбрать качество');self.click('1080p')
        self.click('Выбрать озвучку');self.click('LostFilm');self.click('Включить подписку')
        s=self.w.follow_all()['42'];self.assertTrue(s['enabled']);self.assertEqual((s['quality'],s['voice']),('1080p','LostFilm'))
        self.assertEqual(len(self.w.state['seasons']),1)

    def test_notifications_two_stages_once_for_whole_family(self):
        self.w.state['members']={'456':{'name':'Wife'}}
        s=self.install();s['events']={'2:1':event()}
        self.w.follow_cycle();self.assertEqual(len(s['events']['2:1']['calendar']),2)
        count=len(self.tg.messages);self.w.follow_cycle();self.assertEqual(len(self.tg.messages),count)
        s['events']['2:1']['choices']=[self.offer()];self.w.follow_cycle()
        self.assertEqual(len(s['events']['2:1']['ready']),2)
        self.assertIn('Найдена подходящая',self.tg.messages[-1]);self.assertIn('Новый сезон',self.tg.messages[-1])
        self.store.save();w=Worker(Store(self.store.path),self.q,self.j,self.tg,self.root/'data');w.follow_notify('42',w.follow_all()['42'])
        self.assertEqual(len(self.tg.messages),count+2)

    def test_matching_release_opens_existing_confirm_flow_with_exact_start(self):
        s=self.install();s['events']={'2:1':event([self.offer()])}
        self.w.search_prefs(123).update(quality='2160p',size=0.01)
        self.w.follow_action(123,'follow_event','42','2:1')
        self.click('1 ·');search=self.w.state['searches']['123']
        self.assertEqual((search['title'],search['season'],search['start_episode'],search['auto_season']),('Show',2,1,False))
        self.assertEqual(len(self.w.state['seasons']),1)

    def test_known_episode_is_not_offered_again(self):
        self.w.tick();s=self.install();e=event();e.update(season=1,number=1);s['events']={'1:1':e}
        self.w.follow_cycle();self.assertTrue(e['handled']);self.assertEqual(e['calendar'],{})

    def test_dismissed_or_paused_subscription_is_quiet(self):
        s=self.install();s['enabled']=False;s['events']={'2:1':event()};self.w.follow_cycle();self.assertEqual(self.tg.messages,[])
        s['enabled']=True;self.w.follow_action(123,'follow_dismiss','42','2:1');count=len(self.tg.messages)
        self.w.follow_cycle();self.assertEqual(len(self.tg.messages),count)

    def test_failed_background_scan_does_not_pause_or_stop_media(self):
        s=self.install();s['next_check']=0;self.w.follow_cycle();self.futures[-1].set_exception(APIError('offline'))
        self.w.follow_cycle();self.assertTrue(s['error']);self.assertFalse(self.w.state['paused'])
        self.w.tick();self.assertTrue(any(e['selected'] for e in self.s['episodes']))

    def test_cancel_or_revoke_while_catalog_query_pending_is_safe(self):
        self.command('/follow Show');f=self.futures[-1];self.command('/cancel');count=len(self.tg.messages)
        f.set_result([{'id':42,'name':'Show','premiered':'2025'}]);self.w.follow_cycle()
        self.assertEqual(len(self.tg.messages),count);self.assertEqual(self.w.follow_all(),{})

    def test_late_scan_cannot_overwrite_changed_preferences(self):
        s=self.install();s['next_check']=0;self.w.follow_cycle()
        old=copy.deepcopy(s);self.w.follow_action(123,'follow_set','42','voice','NewStudio')
        self.futures[-1].set_result({'events':{'2:1':event([self.offer()])},'warning':'','next_episode':None,'_generation':old['generation'],'_revision':old['revision']})
        self.w.follow_cycle();self.assertEqual(s['events'],{});self.assertEqual(s['voice'],'NewStudio')

    def test_deleting_pending_scan_does_not_recreate_subscription(self):
        s=self.install();s['next_check']=0;self.w.follow_cycle()
        self.w.follow_action(123,'follow_delete','42');self.futures[-1].set_result({'events':{}});self.w.follow_cycle()
        self.assertEqual(self.w.follow_all(),{})

    def test_notification_budget_and_deleted_generation_buttons(self):
        self.w.state['members']={'456':{'name':'Wife'}}
        s=self.install();s['events']={f'2:{n}':dict(event(),number=n) for n in range(1,7)}
        self.w.follow_cycle();self.assertEqual(sum(len(e['calendar']) for e in s['events'].values()),4)
        self.w.follow_open(123,'42');token=self.buttons()[0]['callback_data']
        self.w.follow_all()['42']=dict(sub(),generation='replacement')
        self.w.callback({'id':'f','from':{'id':123},'data':token,'message':{'message_id':42,'chat':{'id':123,'type':'private'}}})
        self.assertTrue(self.tg.answers[-1][2])

    def test_pack_subscription_uses_existing_library_name(self):
        self.w.ui_action(123,'follow_pack',self.s['hash'])
        d=self.w.state['follow_dialogs']['123'];self.assertEqual(d['library_title'],'Show')
        self.futures[-1].set_result([{'id':42,'name':'Original Show','premiered':'2025-01-01'}]);self.w.follow_cycle();self.click('Original Show')
        self.assertEqual(self.w.follow_all()['42']['title'],'Show')

    def test_notification_delivery_history_survives_inflight_scan(self):
        s=self.install();s['events']={'2:1':event()};s['next_check']=0;self.w.follow_cycle()
        self.futures[-1].set_result({'events':{'2:1':event()},'warning':'','next_episode':None,'_generation':'g','_revision':0})
        count=len(self.tg.messages);self.w.follow_cycle();self.assertEqual(len(self.tg.messages),count)


for name in dir(fixtures.Lifecycle):
    if name.startswith('test_') and name not in Subscriptions.__dict__:setattr(Subscriptions,name,None)

if __name__=='__main__':unittest.main()

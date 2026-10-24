import test from 'node:test';
import assert from 'node:assert/strict';
import {localSettings,limitPatch,synchronizeLimits} from '../dist/settings.js';
test('fresh browser loads authoritative server values, ignores local stale limits',()=>{
 const local=localSettings({server:'',demo:false,pollSeconds:10,episodeLimit:5,reserveGb:5,capGb:null});
 assert.equal('episodeLimit' in local,false);
 const draft={...local};synchronizeLimits(local,draft,{episodeCountLimit:12,reserveGb:18,capGb:80},new Set());
 assert.equal(draft.episodeLimit,12);assert.equal(draft.reserveGb,18);assert.equal(draft.capGb,80);
 assert.deepEqual(limitPatch(draft,new Set()),{});
});
test('polling preserves unsaved edits and patches only intentional changes',()=>{
 const settings={},draft={episodeLimit:7,capGb:null};const dirty=new Set(['episodeLimit','capGb']);
 synchronizeLimits(settings,draft,{episodeCountLimit:12,reserveGb:18,capGb:80},dirty);
 assert.equal(draft.episodeLimit,7);assert.equal(draft.capGb,null);assert.equal(draft.reserveGb,18);
 assert.deepEqual(limitPatch(draft,dirty),{episodeCountLimit:7,capGb:null});
});

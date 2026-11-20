"""Check linked selections with the shared table and the Review callbacks."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Literal, cast

from scripts.review_links import record_table, review_page
from scripts.review_links.test_accounts_page import firefox_probe_results


def save_notice_failures(page: bytes, action: Literal['works', 'remove']) -> list[list[str]]:
    """Check the status region during sequential and overlapping saves."""
    setup = b'''<script>
const realFetch=fetch;
let finishPost;
window.setInterval=()=>0;
window.fetch=(path,options={})=>{
 if(options.method!=='POST'){
  if(new URL(path,location.href).searchParams.has('item-state')){
   return Promise.resolve({ok:true,json:async()=>({item_state:'Working',parked:'false',unread:'true'})});
  }
  return new Promise(()=>{});
 }
 return new Promise((resolve,reject)=>{
  finishPost=reason=>reason ? reject(new Error(reason)) : resolve({ok:true,json:async()=>({
   answer:'works',request:'a'.repeat(32),token:'b'.repeat(64),item_state:'Closed'
  })});
 });
};
</script>'''
    probe = r'''<script>
(async()=>{
 const reloadKey='save-notice-reload';
 const status=document.querySelector('[data-hidden-action-status]');
 const action=ACTION;
 let failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const clear=()=>status.hidden && !status.textContent;
 if(sessionStorage.getItem(reloadKey)){
  failures=JSON.parse(sessionStorage.getItem(reloadKey));
  sessionStorage.removeItem(reloadKey);
  check(clear(),'reload clears the failure');
 }else try{
  if(action==='remove') document.getElementById('filter-show-working').click();
  const [first,second,third,older,newer,next,reload,anchor]=document.querySelectorAll('[data-list-item]');
  const liveRow=row=>document.querySelector('[data-list-item][data-key="'+CSS.escape(row.dataset.key)+'"]');
  const open=row=>{
   row=liveRow(row);
   if(!recordIsOpen(row)) row.querySelector('.list-toggle').click();
   return row;
  };
  const startSave=async row=>{
   const animations=[...document.querySelectorAll('[data-record-motion]')].flatMap(node=>node.getAnimations({subtree:true}));
   for(const animation of animations) animation.finish();
   await Promise.allSettled(animations.map(animation=>animation.finished));
   row=open(row);
   const form=action==='works' ? await readyReviewCard(row) : recordDetails(row).querySelector('form');
   const settled=new Promise(resolve=>{
    const observe=new MutationObserver(()=>{
     if(!form.dataset.sending){observe.disconnect();resolve();}
    });
    observe.observe(form,{attributes:true,attributeFilter:['data-sending']});
   });
   finishPost=undefined;
   form.requestSubmit(form.querySelector('button[value="'+action+'"]'));
   if(!finishPost) throw new Error('The save did not start.');
   check(clear(),'the next save clears the notice at submission');
   const finish=finishPost;
   return {row,form,finish:async reason=>{finish(reason);await settled;}};
  };
  const save=async(row,reason='')=>{
   const save=await startSave(row);
   row=save.row;
   if(!reason){
    const duration=Number(document.querySelector('[data-review],[data-list]').dataset.cardFadeOutMs);
    const motion=row.nextElementSibling ?? [...document.querySelectorAll('[data-record-motion]')].find(
     node=>node.querySelector('.list-toggle').textContent===row.querySelector('.list-toggle').textContent);
    check(row.hidden && motion?.matches('[data-record-motion]') &&
      motion.getAnimations({subtree:true}).some(animation=>animation.effect.getTiming().duration===duration),
      'a successful save starts the configured row fade');
   }else if(action==='works') open(anchor);
   await save.finish(reason);
  };
  const failure=(row,reason)=>{
   // A lost Remove restores the row. Collapse it to check the table notice.
   if(action==='remove' && recordIsOpen(row)) row.querySelector('.list-toggle').click();
   const label=action==='works' ? row.dataset.key : 'Item '+row.dataset.number;
   const prefix=action==='works' ? 'Not saved: ' : 'Save not confirmed: ';
   check(!status.hidden && status.textContent===label+': '+prefix+reason,
    'the table shows only '+reason);
  };
  for(const row of [first,second,third]){
   await save(row);
   check(clear(),'a successful save leaves no notice');
  }
  await save(older,'Fixture first failure');failure(older,'Fixture first failure');
  await save(newer,'Fixture latest failure');failure(newer,'Fixture latest failure');
  await save(next);
  check(clear(),'a later success leaves no notice');
  for(const newerReason of ['Fixture newer overlapping failure','']){
   const olderSave=await startSave(older),newerSave=await startSave(newer);
   if(action==='works') open(anchor);
   await newerSave.finish(newerReason);
   await olderSave.finish('Fixture older overlapping failure');
   if(newerReason){
    failure(newer,newerReason);
    check(newerSave.form.querySelector('[data-send-status]').textContent.includes(newerReason),
     'the newer failure stays on its row');
   }else check(clear(),'an older failure adds no notice after a newer success');
   check(olderSave.form.querySelector('[data-send-status]').textContent.includes('Fixture older overlapping failure'),
    'the older failure stays on its row');
  }
  await save(reload,'Fixture failure before reload');
  failure(reload,'Fixture failure before reload');
  sessionStorage.setItem(reloadKey,JSON.stringify(failures));
  location.reload();
  return;
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''.replace('ACTION', json.dumps(action)).encode()
    return firefox_probe_results(page.replace(b'<head>', b'<head>'+setup, 1).replace(b'</body>', probe+b'</body>'))


class FilterControlTests(unittest.TestCase):
    def test_chip_row_starts_with_all_and_renders_counts(self) -> None:
        row = record_table.chip_row('Owner', (
            record_table.Chip('one', '<span>One</span>', 0),
            record_table.Chip('all', 'All', 2),
            record_table.Chip('two', 'Two'),
        ), 'one')
        self.assertIn('role="group" aria-label="Owner" data-filter-row="owner"', row)
        self.assertEqual([part.split('"', 1)[0] for part in row.split('data-filter-value="')[1:]],
                         ['all', 'one', 'two'])
        self.assertIn('aria-pressed="false" data-filter-count="2">All<b>2</b>', row)
        self.assertIn('aria-pressed="true" data-filter-count="0"><span>One</span><b>0</b>', row)
        self.assertIn('data-filter-count="">Two<b></b>', row)
        self.assertEqual(row,
                         '<div class="record-filter-row" role="group" aria-label="Owner" data-filter-row="owner">'
                         + '<span aria-hidden="true">Owner</span><div>'
                         + '<button type="button" id="filter-owner-all" data-filter-value="all" aria-pressed="false"'
                         + ' data-filter-count="2">All<b>2</b></button>'
                         + '<button type="button" id="filter-owner-one" data-filter-value="one" aria-pressed="true"'
                         + ' data-filter-count="0"><span>One</span><b>0</b></button>'
                         + '<button type="button" id="filter-owner-two" data-filter-value="two" aria-pressed="false"'
                         + ' data-filter-count="">Two<b></b></button></div></div>')
        self.assertIn('aria-label="Filter by &quot;words&quot;"',
                      record_table.words_box('review-search', 'Filter by "words"'))

    def test_control_keeps_zero_chips_and_selects_all_if_selection_is_hidden(self) -> None:
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        result = subprocess.run([node, '-e', record_table.SCRIPT + r'''
const assert=require('node:assert/strict'),row=new EventTarget(),changes=[];
const buttons=['all','one','two'].map(value=>{
 const button=new EventTarget(),attributes={},count={};
 Object.assign(button,{dataset:{filterValue:value},hidden:false,
  setAttribute:(name,text)=>attributes[name]=text,getAttribute:name=>attributes[name],
  querySelector:()=>count});
 return button;
});
Object.assign(row,{querySelectorAll:()=>buttons,hasAttribute:()=>false});
filterControl.bind(row,value=>changes.push(value));
filterControl.setCounts(row,{all:2,one:2});
assert.equal(buttons[2].dataset.filterCount,'0');
assert.equal(buttons[2].querySelector('b').textContent,'0');
assert.equal(buttons[2].hidden,false);
buttons[2].dispatchEvent(new Event('click'));
assert.deepEqual(changes,['two']);
assert.equal(buttons[2].getAttribute('aria-pressed'),'true');
assert.equal(filterControl.setVisible(row,['one']),'all');
assert.equal(buttons[0].hidden,false);
assert.equal(buttons[0].getAttribute('aria-pressed'),'true');
assert.equal(buttons[2].hidden,true);
assert.equal(filterControl.select(row,'two'),'all');
assert.deepEqual(changes,['two']);
'''], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


class LinkedTableTests(unittest.TestCase):
    def render(self, fragment: str, *, closed_item: str = '', park_open_card: bool = False) -> dict[str, object]:
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        _, start, rest = review_page.SCRIPT.partition('function allEntries(){')
        table_script = start + rest.partition('\nasync function afterRowAnimations(')[0]
        source = ('const fragment=' + json.dumps(fragment) + ';\n'
                  + 'const closedItem=' + json.dumps(closed_item) + ';\n'
                  + 'const parkOpenCard=' + json.dumps(park_open_card) + ';\n') + r'''
const element=()=>({hidden:false,textContent:'',dataset:{},value:'',
 setAttribute(key,value){this[key]=value;},getAttribute(key){return this[key] ?? null;},
 toggleAttribute(key,present){if(present)this[key]='';else delete this[key];return present;},
 classList:{toggle(){}},
 addEventListener(type,listener){this[type]=listener;}});
let notice=null;
const empty=element();empty.after=value=>{notice=value;};
const search=element();
const makeChips=(group,values)=>values.map(value=>{
 const count=element();
 return {...element(),dataset:{[group+'Chip']:value},
  getAttribute:name=>name==='data-'+group+'-chip' ? value : null,
  querySelector:selector=>selector==='b' ? count : null};
});
const owners=makeChips('owner',['','one','two']);
const chips=makeChips('type',['','change','question']);
const shows=makeChips('show',['open','Waiting','Done','all']);
const owner={...element(),matches:()=>false,querySelectorAll:()=>owners};
const show={...element(),matches:()=>false,querySelectorAll:()=>shows};
const rows=[
 {key:'sdlc/C1830',state:'Open',type:'change',owner:'one',parked:'false'},
 {key:'hana/Q10',state:'Open',type:'question',owner:'two',parked:'true'},
 {key:'other/C9',state:'Done',type:'change',owner:'two',parked:'false'},
].map((data,index)=>{
 const detail=element(),toggle=element(),row=element();
 const dataset={...data,id:data.key,hiddenUntil:data.parked==='true' ? Date.now()+60000 : 0,
  order:String(index),sortValues:'[]',title:'fixture',number:data.key.toLowerCase()};
 return {...element(),dataset,
  querySelector:selector=>selector==='[data-detail]' ? detail : selector==='.list-toggle' ? toggle : row};
});
const index=new Map(rows.map(({dataset})=>[dataset.key,dataset]));
const table={append(){},addEventListener(){},getAttribute:()=> 'Review'};
const content={
 dataset:{},addEventListener(){},
 querySelector:selector=>({'#list-owner':owner,'#list-show':show,'#list-search':search,
  '[data-empty]':empty,'table':table,'[data-missing-keys]':notice})[selector],
 querySelectorAll:selector=>({'[data-list-item]':rows,'[data-owner-chip]':owners,'[data-type-chip]':chips,'[data-show-chip]':shows})[selector] || [],
};
const document={createElement:element,getElementById:()=>null};
const location={search:'',hash:fragment,pathname:'/console/review.html'};
const history={replaceState(_state,_title,url){this.url=url;}};
const window={},parent=window,pendingSaves=new Map(),saved=new Map();
const reviewPlace=()=>{},loadBodies=()=>{},rememberItem=()=>{};
''' + record_table.SCRIPT + r'''
const createTable=createRecordTable;
{
const createRecordTable=options=>createTable({...options,entries:undefined,mount:undefined});
''' + table_script + r'''
view.place();view.apply(false);
if(parkOpenCard){
 view.opened.add('sdlc/C1830');view.apply(false);
 index.get('sdlc/C1830').hiddenUntil=Date.now()+60000;
 view.apply(false);
}
if(closedItem){
 view.bind();
 view.opened.delete(closedItem);view.apply(false);
 search.value='fixture';search.input({target:search});
}
console.log(JSON.stringify({
 itemInUrl:new URLSearchParams(history.url?.split('#')[1]).get('item'),
 shown:rows.filter(row=>!row.hidden).map(row=>row.dataset.key),
 expanded:rows.filter(recordIsOpen).map(row=>row.dataset.key),
 missing:notice && !notice.hidden ? notice.textContent : '',
 counts:Object.fromEntries(shows.map(chip=>[chip.dataset.showChip,chip.querySelector('b').textContent])),
 chips:Object.fromEntries(chips.map(chip=>[chip.dataset.typeChip,chip.querySelector('b').textContent])),
 owners:Object.fromEntries(owners.map(chip=>{
  const value=chip.dataset.ownerChip;
  return [value,(value || 'All')+' ('+chip.querySelector('b').textContent+')'];
 })),
}));
}
'''
        result = subprocess.run([node, '-e', source], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, object], json.loads(result.stdout))

    def test_an_open_card_leaves_the_view_when_parked_elsewhere(self) -> None:
        result = self.render('#show=all', park_open_card=True)
        self.assertEqual(result['expanded'], ['sdlc/C1830'])
        self.assertEqual(result['shown'], ['other/C9'])

    def test_explicit_item_remains_visible_outside_the_linked_keys(self) -> None:
        result = self.render('#show=all&key=sdlc%2FC1830&item=hana%2FQ10')
        self.assertEqual(result['shown'], ['sdlc/C1830', 'hana/Q10'])
        self.assertEqual(result['expanded'], ['hana/Q10'])

    def test_search_does_not_write_a_closed_review_row_to_the_address(self) -> None:
        result = self.render('#show=all&item=hana%2FQ10', closed_item='hana/Q10')
        self.assertEqual(result['expanded'], [])
        self.assertIsNone(result['itemInUrl'])

    def test_missing_linked_keys_have_a_plain_notice_and_keep_found_cards(self) -> None:
        for keys, shown in (('key=missing%2FC7&key=missing%2FQ8', []),
                            ('key=sdlc%2FC1830&key=missing%2FC7&key=missing%2FQ8', ['sdlc/C1830'])):
            with self.subTest(keys=keys):
                result = self.render('#show=all&' + keys)
                self.assertEqual(result['shown'], shown)
                self.assertEqual(result['missing'], 'missing/C7 is no longer on the Review page.\n'
                                 + 'missing/Q8 is no longer on the Review page.')

    def test_linked_counts_include_selected_parked_cards_and_exclude_other_rows(self) -> None:
        result = self.render('#show=all&key=hana%2FQ10')
        self.assertEqual(result['shown'], ['hana/Q10'])
        self.assertEqual(result['counts'], {'open': 1, 'Waiting': 0, 'Done': 0, 'all': 1})
        self.assertEqual(result['chips'], {'': 1, 'change': 0, 'question': 1})
        self.assertEqual(result['owners'], {'': 'All (1)', 'one': 'one (0)', 'two': 'two (1)'})


if __name__ == '__main__':
    _ = unittest.main()

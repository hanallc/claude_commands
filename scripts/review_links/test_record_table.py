"""Check linked selections with the shared table and the Review callbacks."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import cast

from scripts.review_links import record_table, review_page


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
    def render(self, fragment: str, *, closed_item: str = '') -> dict[str, object]:
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        _, start, callbacks = review_page.SCRIPT.partition('function matchesOwner(')
        callbacks = start + callbacks.split('\nfunction loadBodies()', 1)[0]
        source = ('const fragment=' + json.dumps(fragment) + ';\n'
                  + 'const closedItem=' + json.dumps(closed_item) + ';\n') + r'''
const element=()=>({hidden:false,textContent:'',dataset:{},value:'',
 setAttribute(key,value){this[key]=value;},classList:{toggle(){}},
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
 return {...element(),dataset:{...data,order:String(index),sortValues:'[]',title:'fixture',number:data.key.toLowerCase()},
  querySelector:selector=>selector==='[data-detail]' ? detail : selector==='.list-toggle' ? toggle : row};
});
const index=new Map(rows.map(({dataset})=>[dataset.key,{...dataset,id:dataset.key,
 hiddenUntil:dataset.parked==='true' ? Date.now()+60000 : 0}]));
const table={append(){},getAttribute:()=> 'Review'};
const content={
 addEventListener(){},
 querySelector:selector=>({'#list-owner':owner,'#list-show':show,'#list-search':search,
  '[data-empty]':empty,'table':table,'[data-missing-keys]':notice})[selector],
 querySelectorAll:selector=>({'[data-list-item]':rows,'[data-owner-chip]':owners,'[data-type-chip]':chips,'[data-show-chip]':shows})[selector] || [],
};
const document={createElement:element};
const location={search:'',hash:fragment,pathname:'/console/review.html'};
const history={replaceState(_state,_title,url){this.url=url;}};
const window={},parent=window,departures=new Map();
let selectedShow='all',requestedShow='all';
const reviewPlace=()=>{},loadBodies=()=>{},pageRefresh={refresh(){}};
''' + record_table.SCRIPT + '\n' + callbacks + r'''
view.place();view.apply(false);
if(closedItem){
 view.bind();
 view.opened.delete(closedItem);view.apply(false);
 search.value='fixture';search.input({target:search});
}
console.log(JSON.stringify({
 itemInUrl:new URLSearchParams(history.url?.split('#')[1]).get('item'),
 shown:rows.filter(row=>!row.hidden).map(row=>row.dataset.key),
 expanded:rows.filter(row=>!row.querySelector('[data-detail]').hidden).map(row=>row.dataset.key),
 missing:notice && !notice.hidden ? notice.textContent : '',
 counts:Object.fromEntries(shows.map(chip=>[chip.dataset.showChip,chip.querySelector('b').textContent])),
 chips:Object.fromEntries(chips.map(chip=>[chip.dataset.typeChip,chip.querySelector('b').textContent])),
 owners:Object.fromEntries(owners.map(chip=>{
  const value=chip.dataset.ownerChip;
  return [value,(value || 'All')+' ('+chip.querySelector('b').textContent+')'];
 })),
}));
'''
        result = subprocess.run([node, '-e', source], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, object], json.loads(result.stdout))

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

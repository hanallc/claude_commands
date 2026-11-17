"""Check linked selections with the shared table and the Review callbacks."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import cast

from scripts.review_links import record_table, review_page


class LinkedTableTests(unittest.TestCase):
    def render(self, fragment: str, *, closed_item: str = '') -> dict[str, object]:
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        callbacks = review_page.SCRIPT.split('const view=createRecordTable(', 1)[1].split('\nfunction loadBodies()', 1)[0]
        source = ('const fragment=' + json.dumps(fragment) + ';\n'
                  + 'const closedItem=' + json.dumps(closed_item) + ';\n') + r'''
const element=()=>({hidden:false,textContent:'',dataset:{},value:'',
 setAttribute(key,value){this[key]=value;},classList:{toggle(){}},
 addEventListener(type,listener){this[type]=listener;}});
let notice=null;
const empty=element();empty.after=value=>{notice=value;};
const count=element(),show=element(),search=element();
const owners=['','one','two'].map(value=>({...element(),value,dataset:{ownerName:value || 'All'}}));
const owner={...element(),options:owners};
const chips=['','change','question'].map(type=>({...element(),dataset:{typeChip:type},
 querySelector:()=>element()}));
for(const chip of chips){const number=element();chip.querySelector=()=>number;}
const rows=[
 {key:'sdlc/C1830',state:'For you',type:'change',owner:'one',parked:'false'},
 {key:'hana/Q10',state:'For you',type:'question',owner:'two',parked:'true'},
 {key:'other/C9',state:'Done',type:'change',owner:'two',parked:'false'},
].map((data,index)=>{
 const detail=element(),toggle=element(),row=element();
 return {...element(),dataset:{...data,order:String(index),sortValues:'[]',title:'fixture',number:data.key.toLowerCase()},
  querySelector:selector=>selector==='[data-detail]' ? detail : selector==='.list-toggle' ? toggle : row};
});
const table={append(){},getAttribute:()=> 'Review'};
const content={
 addEventListener(){},
 querySelector:selector=>({'#list-owner':owner,'#list-show':show,'#list-search':search,
  '[data-empty]':empty,'[data-list-count]':count,'table':table,'[data-missing-keys]':notice})[selector],
 querySelectorAll:selector=>selector==='[data-list-item]' ? rows : selector==='[data-type-chip]' ? chips : [],
};
const document={createElement:element};
const location={search:'',hash:fragment,pathname:'/console/review.html'};
const history={replaceState(_state,_title,url){this.url=url;}};
const window={},parent=window,saved=new Map();
let selectedShow='all',requestedShow='all';
const reviewPlace=()=>{},loadBodies=()=>{},pageRefresh={refresh(){}};
''' + record_table.SCRIPT + '\nconst view=createRecordTable(' + callbacks + r'''
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
 counts:count.textContent,
 chips:Object.fromEntries(chips.map(chip=>[chip.dataset.typeChip,chip.querySelector('b').textContent])),
 owners:Object.fromEntries(owners.map(option=>[option.value,option.textContent])),
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
        self.assertEqual(result['counts'], '1 for you · 0 waiting · 0 done')
        self.assertEqual(result['chips'], {'': 1, 'change': 0, 'question': 1})
        self.assertEqual(result['owners'], {'': 'All (1)', 'one': 'one (0)', 'two': 'two (1)'})


if __name__ == '__main__':
    _ = unittest.main()

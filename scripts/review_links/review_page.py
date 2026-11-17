"""Show questions, changes and investigations in one review table."""
from __future__ import annotations

import html
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, investigation, staff, update_registration
from scripts.review_links import change_browser, change_page, console, page_frame, record_table, review_items, review_link
from scripts.review_links.review_items import ReviewItem, ReviewState, ReviewType

PAGE_PATH = '/console/review.html'
HEADERS = ('Id', 'Type', 'Owner', 'Item', 'State', 'Age')
TYPES = (ReviewType.QUESTION, ReviewType.CHANGE, ReviewType.INVESTIGATION)
ICONS = {ReviewType.QUESTION: 'circle-help', ReviewType.CHANGE: 'diff', ReviewType.INVESTIGATION: 'microscope'}
SHOWS = (('open', 'Open'), ('For you', 'For you'), ('Waiting', 'Waiting'), ('Done', 'Done'), ('all', 'All'))
LABELS = {ReviewType.QUESTION: 'Questions', ReviewType.CHANGE: 'Changes', ReviewType.INVESTIGATION: 'Investigations'}


def page_url() -> str:
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def waiting_items(request: console.NewsRequest) -> frozenset[str]:
    now = datetime.now(user_zone())
    return frozenset(item.key+'/'+item.times.updated.isoformat() for item in request.shared(review_items.read) if item.for_you(now))


def legacy_url(path: str, query: str) -> str:
    kind = 'investigation' if path.endswith('investigations.html') else 'question' if 'type=questions' in query else 'change'
    return PAGE_PATH + '#' + urlencode({'type': kind})


def icon(kind: ReviewType) -> str:
    return '<span class="review-icon '+kind+'">'+page_frame.icon_svg(ICONS[kind])+'</span>'


def section(heading: str, text: str) -> str:
    return '<section><h3>'+heading+'</h3>'+change_browser.question_paragraphs(text)+'</section>'


def form(item: ReviewItem, browser: change_page.ReviewBrowser) -> str:
    if not item.actions:
        return ''
    fields = {'row': item.key, 'token': change_browser.row_token(browser, item.key)}
    hidden = ''.join(f'<input type="hidden" name="{key}" value="{html.escape(value, quote=True)}">' for key, value in fields.items())
    closes: list[str] = []
    returns: list[str] = []
    labels = {'answer': 'Answer', 'works': 'Works', 'not works': 'Does not work', 'validate': 'Ask showrunner to check',
              'question': 'Ask a question', 'feedback': 'Send feedback', 'read': 'Mark read'}
    for index, action in enumerate(item.actions):
        button = f'<button name="outcome" value="{action}"'+(' class="primary"' if index == 0 else '')+'>'+labels[action]+'</button>'
        (returns if action == 'question' else closes).append(button)
    groups = '<div class="review-action-group"><span>Closes the item</span><div>'+''.join(closes)+'</div></div>'
    if returns:
        groups += '<div class="review-action-group"><span>The item returns to you</span><div>'+''.join(returns)+'</div></div>'
    return ('<form method="post" data-review-form>'+hidden+f'<label for="words-{html.escape(item.key, quote=True)}">Your words</label>'
            + f'<textarea id="words-{html.escape(item.key, quote=True)}" name="feedback" rows="2" '
            + 'placeholder="Type your words. They go with the button that you press."></textarea>'
            + '<div class="review-actions">'+groups+'</div><span role="status" data-send-status></span></form>')


def details(item: ReviewItem, browser: change_page.ReviewBrowser, members: staff.StaffSnapshot) -> str:
    source = item.source
    if isinstance(source, review_items.QuestionSource):
        content, choices = change_browser.question_parts(source.record.item)
        question = source.record.item
        ask = question['parts']['ask'] if 'parts' in question else change_browser.question_text(question)[0]
        lead = change_page.review_text_html(ask, inline=True) if ask != question['text'] else ''
        body = '<section><h3>Question</h3>'+lead+content+choices+'</section>'
    elif isinstance(source, review_items.ChangeSource):
        row = source.row
        body = section('How to check', row.get('How to check', ''))+section('Proof', row.get('What you see', '') or row.get('Proof', ''))
        if isinstance(source.slots.question, change_page.AnsweredQuestion):
            body += section('Answer', source.slots.question.answer)
        image_id = row.get('Image', '')
        if image_id:
            body = change_page.change_content(row, body)
    elif isinstance(source, review_items.InvestigationSource):
        body = section('You asked', source.record.question)
        if source.record.answer:
            body += section('Answer', source.record.answer)
    else:
        body = ''
    logs = ''.join('<li><time datetime="'+entry.time.isoformat()+'">'
        +entry.time.astimezone(user_zone()).strftime('%b %d %H:%M')+'</time><span>'+html.escape(entry.words)
        +' <small>— '+html.escape(entry.who)+'</small></span></li>' for entry in reversed(item.log))
    owner = review_items.name(item.owner, members)
    return ('<div class="list-panel review-panel '+item.type+'"><div>'+body+'</div><section><h3>Log · newest first</h3>'
            +'<ol class="list-log">'+logs+'</ol></section>'+form(item, browser)
            +'<p class="review-source">Owner: '+html.escape(owner)+' · Source: '
            +html.escape(review_items.name(item.source_unit, members))+'</p></div>')


def age(filed: datetime, now: datetime) -> str:
    seconds = max(0, int((now-filed).total_seconds()))
    return f'{seconds//86400} d' if seconds >= 86400 else f'{seconds//3600} h' if seconds >= 3600 else f'{seconds//60} min'


def item_rows(item: ReviewItem, browser: change_page.ReviewBrowser, members: staff.StaffSnapshot, now: datetime, index: int) -> str:
    owner = review_items.name(item.owner, members)
    source = review_items.name(item.source_unit, members)
    title = '<strong>'+html.escape(item.title)+'</strong>'
    if item.urgency.level != 'normal':
        title += '<small>'+html.escape({'user': 'Important', 'blocks': 'Blocks work', 'soon': 'Due soon', 'later': 'Later'}[item.urgency.level])+'</small>'
    values = (item.id, item.type.value, owner, item.title, item.state.value, item.times.filed.timestamp())
    attrs = {'key':item.key, 'number':item.id.casefold(), 'title':item.title.casefold(), 'owner':str(item.owner),
             'state':item.state.value, 'type':item.type.value, 'order':str(index), 'sort-values':json.dumps(values),
             'parked':str(item.hidden_until(now)).lower(), 'updated':item.times.updated.isoformat(),
             'checking':str(isinstance(item.source, review_items.ChangeSource) and item.source.slots.validation_requested).lower()}
    attributes = ''.join(f' data-{key}="{html.escape(value, quote=True)}"' for key, value in attrs.items())
    panel = 'review-detail-'+str(index)
    toggle = (f'<button type="button" class="list-toggle" aria-expanded="false" aria-controls="{panel}">'
              +'<span class="list-chevron" aria-hidden="true">›</span>'+html.escape(item.id)+'</button>')
    state = '<span class="list-state '+('warning' if item.state == ReviewState.FOR_YOU else 'muted')+'">'+item.state+'</span>'
    cells = (toggle, icon(item.type)+item.type.title(), html.escape(owner)+('<small>· '+html.escape(source)+'</small>' if source != owner else ''),
             title, state, age(item.times.filed, now))
    row = ''.join(f'<td data-label="{label}">{cell}</td>' for label, cell in zip(HEADERS, cells))
    hidden = ' hidden' if item.state == ReviewState.DONE or item.hidden_until(now) else ''
    return ('<tbody data-list-item'+attributes+hidden+'><tr class="list-row">'+row+'</tr>'
            +f'<tr data-detail id="{panel}" hidden><td colspan="6">'+details(item, browser, members)+'</td></tr></tbody>')


def render(root: Path) -> bytes:
    started, read_at = time.perf_counter(), time.time()
    members = staff.snapshot()
    now = datetime.fromtimestamp(read_at, user_zone())
    items = [item for item in review_items.read(root, members=members) if item.type in TYPES]
    items.sort(key=lambda item: (item.state != ReviewState.FOR_YOU, item.state == ReviewState.DONE))
    token, initial = change_browser.browser_manifest(change_page.console_folder(root))
    browser = change_page.ReviewBrowser(token, initial)
    visible = [item for item in items if not item.hidden_until(now) and item.state != ReviewState.DONE]
    owners = {item.owner:review_items.name(item.owner, members) for item in items}
    options = ''.join(f'<option value="{html.escape(owner, quote=True)}" data-owner-name="{html.escape(name, quote=True)}">{html.escape(name)} '
                     +f'({sum(item.owner == owner for item in visible)})</option>'
                     for owner, name in sorted(owners.items(), key=lambda pair:pair[1].casefold()))
    chips = '<button type="button" data-type-chip="" aria-pressed="true">All <b>'+str(len(visible))+'</b></button>'
    chips += ''.join('<button type="button" data-type-chip="'+kind+'" aria-pressed="false">'+icon(kind)+LABELS[kind]
                     +' <b>'+str(sum(item.type == kind for item in visible))+'</b></button>' for kind in TYPES)
    shows = ''.join(f'<option value="{value}">{label}</option>' for value, label in SHOWS)
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
                      +'<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    content = (record_table.STYLE+STYLE+page_frame.title('Review', 'review')
        +f'<div data-review data-refresh-seconds="{conf.read("review_links").number("page_refresh_seconds")}">'
        +'<div class="review-types">'+chips+'</div><div class="list-filters">'
        +'<label for="list-owner">Owner</label><select id="list-owner"><option value="">All ('+str(len(visible))+')</option>'+options+'</select>'
        +'<label for="list-show">Show</label><select id="list-show">'+shows+'</select>'
        +'<label class="list-search-label" for="list-search">Search</label><input type="search" id="list-search" placeholder="Filter by id or words">'
        +'<span data-list-count role="status"></span></div><div class="list-table"><table aria-label="Review">'
        +'<colgroup><col style="width:13%"><col style="width:13%"><col style="width:14%"><col style="width:41%">'
        +'<col style="width:12%"><col style="width:7%"></colgroup><thead><tr>'+headers+'</tr></thead>'
        +''.join(item_rows(item,browser,members,now,index) for index,item in enumerate(items))
        +'</table><p data-empty hidden>No items in this view.</p></div></div><script>'+SCRIPT+'</script>')
    return page_frame.rendered(change_page.document(content, 'Review'), started, read_at)


def submit(root: Path, body: str, status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {'row','token','outcome','feedback'} or any(len(values)!=1 for values in fields.values()):
        raise change_page.AnswerRefused('use the answer form on this page')
    token, initial = change_browser.browser_manifest(change_page.console_folder(root))
    browser = change_page.ReviewBrowser(token,initial)
    key = fields['row'][0]
    if not secrets.compare_digest(fields['token'][0],change_browser.row_token(browser,key)):
        raise change_page.AnswerRefused('row or page token does not match this page')
    if key.startswith('investigation/'):
        _, owner, identity = key.split('/')
        if fields['outcome'] != ['read']:
            raise change_page.AnswerRefused('choose Mark read')
        _ = investigation.mark_read(staff.StaffId(owner),identity)
        return change_page.NoMessageDelivery(), 'marked read'
    delivery, answer = change_browser.submit(browser, urlencode({key:values[0] for key,values in fields.items()}), status,
                                               feedback_closes=True)
    words = fields['outcome'][0]+(': '+fields['feedback'][0] if fields['feedback'][0] else '')
    if isinstance(delivery, change_page.MessageDelivery):
        if isinstance(delivery.page, change_page.UserQuestionPage):
            update_registration.record_review_log(delivery.page.review_list, words)
        else:
            change_review.record_review_log(delivery.page.review_list, delivery.row, words, delivery.page.production)
    return delivery, answer or {'validate':'asked the showrunner to check', 'question':'question sent', 'feedback':'feedback sent'}.get(fields['outcome'][0],'answer')


STYLE = '''<style>
.review-types{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.review-types button{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:14px;min-height:36px;
background:var(--card);border:1px solid var(--line);border-radius:999px;color:var(--ink-2);padding:5px 14px;margin:0}
.review-types button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.review-icon{display:inline-flex;vertical-align:middle;margin-right:5px}.review-icon svg{width:18px;height:18px}
.question{--type-color:#b99aff}.change{--type-color:#62b7ef}.investigation{--type-color:#30c1ad}
.review-icon{color:var(--type-color)}.review-panel{border-left-color:var(--type-color);grid-template-columns:1.25fr 1fr;padding:0 20px 18px 58px;gap:4px 28px}
.review-panel [data-review-form]{grid-column:1/-1;border-top:1px solid var(--line);padding-top:12px;margin-top:10px}
[data-review-form] label{display:block;font-weight:600;margin-bottom:6px}
[data-review-form] textarea{display:block;box-sizing:border-box;width:100%;font:inherit;padding:9px 12px;background:var(--bg);
color:var(--ink);border:1px solid var(--line);border-radius:8px;resize:vertical}
.review-actions{display:flex;align-items:flex-end;gap:26px;margin-top:12px;flex-wrap:wrap}
.review-action-group>span{display:block;font-size:11px;color:var(--ink-3);text-transform:uppercase;margin-bottom:5px}
.review-action-group>div{display:flex;gap:8px;flex-wrap:wrap}.review-action-group+.review-action-group{padding-left:26px;border-left:1px solid var(--line)}
.review-actions button{margin:0;font:inherit;min-height:40px;padding:6px 14px;border:1px solid var(--ink-3);border-radius:8px;
background:var(--card);color:var(--ink)}.review-actions .primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.review-source{grid-column:1/-1;color:var(--ink-3);font-size:12px;margin-top:16px!important}
.list-table .list-row td:last-child{text-align:right}.list-table [data-detail]>td{text-align:left}
@media(max-width:960px){[data-review] .list-row td:last-child::before{content:'Age: '}
.review-panel{grid-template-columns:1fr;padding:0 14px 16px}.review-action-group+.review-action-group{padding-left:0;border:0}}
</style>'''

SCRIPT = page_frame.REFRESH_SCRIPT + record_table.SCRIPT + r'''(() => {
let content=document.querySelector('[data-review]'),saving=0;
const drafts=new Map(),saved=new Map();
const view=createRecordTable({content:()=>content,single:true,
 resolveOwner:owner=>[...content.querySelectorAll('#list-owner option')].find(option=>option.value===owner || option.dataset.ownerName===owner)?.value ?? owner,
 shows:['open','For you','Waiting','Done','all'],
 visible:(data,{owner,show,query,type})=>(show==='all' || !saved.has(data.key) && data.parked!=='true') &&
  !(show==='open' && data.state==='Waiting' && data.checking==='true') &&
  (!owner || data.owner===owner) && (!type || data.type===type) &&
  (!query || data.number.includes(query) || data.title.includes(query)) &&
  (show==='all' || (show==='open' ? data.state!=='Done' : data.state===show)),
 counts:items=>['For you','Waiting','Done'].map(state=>items.filter(item=>item.dataset.state===state && item.dataset.parked!=='true').length+' '+state.toLowerCase()).join(' · '),
 applied:(items,{type})=>content.querySelectorAll('[data-type-chip]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.typeChip===type)))
});
function bind(){
 view.replace();
 content.querySelectorAll('[data-review-form]').forEach(form=>{
  const key=form.closest('[data-list-item]').dataset.key,words=form.elements.feedback;
  if(drafts.has(key)) words.value=drafts.get(key);
  form.closest('[data-list-item]').querySelectorAll('input[name=choice]').forEach(choice=>choice.addEventListener('change',()=>{words.value=choice.value;drafts.set(key,words.value);words.focus();}));
  words.addEventListener('input',()=>drafts.set(key,words.value));
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(form.dataset.sending) return;
   const data=new URLSearchParams(new FormData(form));data.set('outcome',event.submitter.value);
   form.dataset.sending='true';saving++;pageRefresh.abandon();
   const buttons=[...form.querySelectorAll('button')],notice=form.querySelector('[data-send-status]');
   buttons.forEach(button=>button.disabled=true);notice.textContent='Saving…';
   try{
    const response=await fetch(location.pathname,{method:'POST',body:data});
    const result=await response.json();if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
    notice.textContent='Saved: '+result.answer;drafts.delete(key);
    await new Promise(resolve=>setTimeout(resolve,900));
    saved.set(key,form.closest('[data-list-item]').dataset.updated);view.opened.delete(key);view.apply(false);
   }catch(error){notice.textContent='Not saved: '+error.message;buttons.forEach(button=>button.disabled=false);}
   finally{delete form.dataset.sending;saving--;}
  });
 });
}
async function refresh(_supersede=false,current=()=>true){
 if(saving) return;
 const response=await fetch(location.pathname,{cache:'no-store'});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const page=new DOMParser().parseFromString(await response.text(),'text/html'),fresh=page.querySelector('[data-review]');
 if(!fresh) throw new Error('The page has no review table.');
 if(saving || !current()) return;
 const top=scrollY,focus=document.activeElement?.id,start=document.activeElement?.selectionStart,end=document.activeElement?.selectionEnd;
 content.replaceWith(fresh);content=fresh;
 for(const [key,updated] of saved){const row=content.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');if(!row || row.dataset.state!=='For you' || row.dataset.updated!==updated) saved.delete(key);}
 bind();view.apply(false);
 if(focus){const field=document.getElementById(focus);field?.focus({preventScroll:true});
  if(typeof field?.setSelectionRange==='function' && typeof start==='number' && typeof end==='number') field.setSelectionRange(start,end);}
 scrollTo({top,behavior:'instant'});return pageRefreshFacts(page);
}
view.place();bind();view.apply();
addEventListener('hashchange',()=>{view.place();view.apply(false);});
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
})();'''

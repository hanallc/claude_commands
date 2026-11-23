"""Check the shared console setting controls."""

from decimal import Decimal
import shutil
import unittest

from scripts.review_links import console_theme, page_frame, setting_controls
from scripts.review_links.test_accounts_page import firefox_probe_results


def cost_limit(value: str, live: str) -> str:
    return setting_controls.slider("limit", "Cost limit for gates",
                                   minimum=Decimal("1.00"), maximum=Decimal("3.00"), step=Decimal("0.05"),
                                   value=Decimal(value), live=Decimal(live), decimals=2,
                                   live_note="off" if Decimal(live) == 1 else "")


def budget() -> str:
    return setting_controls.amount_box("budget", "I am willing to pay up to", "more a month to go faster.",
                                       minimum=Decimal("0"), step=Decimal("0.01"), value=Decimal("31.36"))


def go() -> str:
    return setting_controls.go_button("limit-go", "Set the limit to 1.50 for gates",
                                      page="aws_machines", key="faster_box_max_cost_ratio")


GO_PROBE = r"""<script>
(async()=>{
 const failures=[];
 const check=(condition,message)=>{if(!condition) failures.push(message);};
 const realFetch=window.fetch;
 try{
  const root=document.getElementById('limit-go'),limit=document.getElementById('limit');
  const amount=document.getElementById('budget');
  const slider=pageSettingControls.connectSlider(limit);
  const send=root.querySelector('[data-setting-go]'),confirm=root.querySelector('[data-setting-save]');
  const cancel=root.querySelector('[data-setting-cancel]'),line=root.querySelector('[data-setting-confirm]');
  const summary=root.querySelector('[data-setting-summary]'),now=limit.querySelector('.setting-slider-now');
  let calls=0,posted,finish,reportPost;
  const nextPost=()=>new Promise(resolve=>{reportPost=resolve;});
  window.fetch=(path,options)=>{
   check(path==='/console/tune','The Go button uses the shared route.');
   if(!options.method) return Promise.resolve({ok:true,json:async()=>({token:'fixture-token'})});
   calls++;
   posted=new URLSearchParams(options.body);
   check(options.method==='POST','Confirm sends a POST.');
   return new Promise(resolve=>{finish=resolve;reportPost();});
  };
  pageSettingControls.connectGo(root,slider,amount,()=>({
   newValue:slider.format(slider.value),monthlyChange:14.58
  }));

  const luminance=rgb=>{
   const channels=rgb.match(/\d+/g).map(value=>Number(value)/255);
   return channels.map(channel=>channel<=.04045 ? channel/12.92 : ((channel+.055)/1.055)**2.4)
    .reduce((sum,channel,index)=>sum+channel*[.2126,.7152,.0722][index],0);
  };
  for(const theme of ['light','dark']){
   document.documentElement.dataset.theme=theme;
   for(const button of [send,confirm]){
    const style=getComputedStyle(button);
    const ink=luminance(style.color),background=luminance(style.backgroundColor);
    check((ink+.05)/(background+.05)>=4.5,theme+' Go and Confirm text meets 4.5:1.');
   }
  }

  const track=limit.querySelector('.setting-slider-track');
  const tick=limit.querySelector('.setting-slider-tick');
  for(const [value,percent] of [[1,0],[2,50],[3,100]]){
   slider.setValue(value);
   slider.setLive(value);
   const rect=track.getBoundingClientRect();
   check(limit.style.getPropertyValue('--setting-fill')===percent+'%', 'The fill reaches '+percent+'%.');
   check(Math.abs(tick.getBoundingClientRect().left-rect.left-rect.width*percent/100)<1,
    'The live mark reaches '+percent+'%.');
  }
  slider.setValue(1.50);
  slider.setLive(1,'off');
  send.click();
  check(!line.hidden && summary.textContent==='Change from 1.00 to 1.50. The estimate changes by +$14.58 a month.',
   'Go shows one confirmation with both values and the monthly change.');
  cancel.click();
  check(line.hidden && calls===0 && document.activeElement===send,'Cancel closes the line and returns focus.');
  send.click();
  const saved=new Promise(resolve=>root.addEventListener('setting-saved',resolve,{once:true}));
  let post=nextPost();
  confirm.click();
  const controls=[send,confirm,cancel,slider.input,amount];
  check(controls.every(control=>control.disabled),'All controls are disabled during the post.');
  await post;
  check(calls===1 && posted.get('token')==='fixture-token' && posted.get('page')==='aws_machines' && posted.get('key')==='faster_box_max_cost_ratio'
   && posted.get('value')==='1.5','Confirm sends the current choice once.');
  finish({ok:true,json:async()=>({state:'saved',live_value:'1.50'})});
  await saved;
  check(line.hidden && controls.every(control=>!control.disabled),'Success restores the controls.');
  check(now.textContent==='Now 1.50' && limit.style.getPropertyValue('--setting-live')==='25%',
   'Success moves the live mark to the saved value.');

  let changes=0;
  amount.addEventListener('change',()=>{changes++;slider.setValue(amount.valueAsNumber/10);});
  amount.value='20.00';
  amount.dispatchEvent(new Event('change',{bubbles:true}));
  check(changes===1 && slider.value===2,'The amount change can set the slider.');
  send.click();
  post=nextPost();
  confirm.click();
  await post;
  finish({ok:false,json:async()=>({reason:'The setting is held.',live_value:'1.50'})});
  await new Promise(resolve=>requestAnimationFrame(resolve));
  check(root.querySelector('[data-setting-error]').textContent==='The setting is held.',
   'A refusal shows its reason.');
  check(slider.value===2 && slider.live===1.5 && now.textContent==='Now 1.50',
   'A refusal keeps the choice and live mark.');
  send.click();
  post=nextPost();
  confirm.click();
  await post;
  finish({ok:false,json:async()=>({reason:'The setting stays live.',live_value:'2.00'})});
  await new Promise(resolve=>requestAnimationFrame(resolve));
  check(root.querySelector('[data-setting-error]').textContent==='The setting stays live.',
   'A publish failure shows its reason.');
  check(slider.value===2 && slider.live===2 && now.textContent==='Now 2.00'
   && limit.style.getPropertyValue('--setting-live')==='50%','The live mark follows the server after a publish failure.');
  slider.setValue(2.25);
  send.click();
  check(summary.textContent.startsWith('Change from 2.00 to 2.25.'),'The next confirmation uses the current live value.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""


class SettingControlsTests(unittest.TestCase):
    def test_slider_renders_bounds_value_and_live_position(self) -> None:
        for choice, live, fill, mark in (("1.00", "3.00", "0%", "100%"), ("2.00", "2.00", "50%", "50%"),
                                         ("3.00", "1.00", "100%", "0%")):
            with self.subTest(choice=choice, live=live):
                markup = cost_limit(choice, live)
                self.assertIn(f'--setting-fill:{fill};--setting-live:{mark}', markup)
                self.assertIn(f'min="1.00" max="3.00" step="0.05" value="{choice}"', markup)
                self.assertIn(f'data-slider-value>{choice}</output>', markup)
                self.assertIn(f'Now {live}' + (', off' if live == "1.00" else '') + '</span>', markup)

    def test_amount_box_and_go_render_the_supplied_words(self) -> None:
        self.assertIn('for="budget"', budget())
        self.assertIn('I am willing to pay up to</span>', budget())
        self.assertIn('<span>$</span>', budget())
        self.assertIn('min="0" step="0.01" value="31.36"', budget())
        self.assertIn('more a month to go faster.', budget())
        self.assertIn('data-page="aws_machines" data-key="faster_box_max_cost_ratio"', go())
        self.assertIn('Set the limit to 1.50 for gates</button>', go())

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the shared controls.")
    def test_go_confirms_cancels_saves_and_reports_a_refusal(self) -> None:
        markup = page_frame.template(
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><style>' + console_theme.TOKENS
            + '</style>__PAGE_SETTING_CONTROLS__</head><body>' + cost_limit("1.50", "1.00") + budget() + go() + GO_PROBE + '</body></html>',
            "AWS Machines", "aws_machines")
        self.assertEqual(markup.count(setting_controls.SCRIPT), 1)
        self.assertEqual(markup.count(setting_controls.STYLE), 1)
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

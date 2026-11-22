"""Render the shared Slider, Amount box and Go button."""

from decimal import Decimal
from html import escape


def slider(identity: str, label: str, *, minimum: Decimal, maximum: Decimal, step: Decimal,
           value: Decimal, live: Decimal, decimals: int, live_note: str = "") -> str:
    """Render the choice and the current setting on the same scale."""
    identity = escape(identity)
    chosen = f"{value:.{decimals}f}"
    current = f"{live:.{decimals}f}" + (f", {live_note}" if live_note else "")
    fill = _position(value, minimum, maximum)
    mark = _position(live, minimum, maximum)
    return (
        f'<div id="{identity}" class="setting-slider" data-decimals="{decimals}" data-live="{live:f}" '
        f'style="--setting-fill:{fill};--setting-live:{mark}">'
        f'<label for="{identity}-value">{escape(label)}</label>'
        f'<output for="{identity}-value" data-slider-value>{chosen}</output>'
        '<div class="setting-slider-scale">'
        '<span class="setting-slider-track" aria-hidden="true"></span>'
        f'<input type="range" id="{identity}-value" min="{minimum:f}" max="{maximum:f}" step="{step:f}" '
        f'value="{value:f}" aria-valuetext="{chosen}" aria-describedby="{identity}-now">'
        '<div class="setting-slider-marks">'
        '<span class="setting-slider-tick" aria-hidden="true"></span>'
        f'<span class="setting-slider-now" id="{identity}-now">Now {escape(current)}</span>'
        '</div></div></div>'
    )


def _position(value: Decimal, minimum: Decimal, maximum: Decimal) -> str:
    percent = Decimal(100) * (value - minimum) / (maximum - minimum)
    return f"{min(Decimal(100), max(Decimal(0), percent)):.8f}".rstrip("0").rstrip(".") + "%"


def amount_box(identity: str, before: str, after: str, *,
               minimum: Decimal, step: Decimal, value: Decimal) -> str:
    """Render a USD number field in a sentence."""
    return (
        f'<label for="{escape(identity)}" class="setting-amount">'
        f'<span>{escape(before)}</span>'
        '<span class="setting-amount-box"><span>$</span>'
        f'<input type="number" inputmode="decimal" id="{escape(identity)}" '
        f'min="{minimum:f}" step="{step:f}" value="{value:f}"></span>'
        f'<span>{escape(after)}</span></label>'
    )


def go_button(identity: str, label: str, *, page: str, key: str) -> str:
    """Render the Go button, confirmation and result line."""
    identity = escape(identity)
    return (
        f'<div id="{identity}" class="setting-go" data-page="{escape(page)}" data-key="{escape(key)}">'
        f'<button type="button" data-setting-go aria-expanded="false" aria-controls="{identity}-confirm">'
        f'{escape(label)}</button>'
        f'<div id="{identity}-confirm" class="setting-confirm" data-setting-confirm hidden>'
        '<span data-setting-summary></span>'
        '<button type="button" data-setting-save>Confirm</button>'
        '<button type="button" data-setting-cancel>Cancel</button>'
        '</div><p class="setting-error" role="alert" data-setting-error></p></div>'
    )


STYLE = r'''
.setting-slider>label{display:block;color:var(--ink-2);font-size:.875rem}
.setting-slider>output{display:block;margin:2px 0 8px;font-size:1.9rem;font-weight:700;font-variant-numeric:tabular-nums}
.setting-slider-scale{position:relative}
.setting-slider-track{position:absolute;inset:12px 10px auto;height:6px;border-radius:3px;pointer-events:none;
background:linear-gradient(90deg,var(--accent) var(--setting-fill),var(--line) var(--setting-fill))}
.setting-slider input{display:block;position:relative;appearance:none;width:100%;height:30px;margin:0;padding:0;
border:0;background:transparent;cursor:pointer}
.setting-slider input::-webkit-slider-runnable-track{height:6px;background:transparent}
.setting-slider input::-webkit-slider-thumb{appearance:none;box-sizing:border-box;width:20px;height:20px;margin-top:-7px;
border:3px solid var(--accent);border-radius:50%;background:var(--ink)}
.setting-slider input::-moz-range-track{height:6px;background:transparent}
.setting-slider input::-moz-range-thumb{box-sizing:border-box;width:20px;height:20px;
border:3px solid var(--accent);border-radius:50%;background:var(--ink)}
.setting-slider input:disabled{cursor:default;opacity:.6}
.setting-slider-marks{position:relative;margin:0 10px;height:26px;color:var(--ink-2);font-size:.8125rem;font-weight:600}
.setting-slider-tick,.setting-slider-now{position:absolute;left:var(--setting-live)}
.setting-slider-tick{top:-4px;width:1px;height:8px;background:currentColor}
.setting-slider-now{top:6px;transform:translateX(calc(-1 * var(--setting-live)));white-space:nowrap;
font-variant-numeric:tabular-nums}
.setting-amount{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 8px;color:var(--ink-2);font-size:.9375rem}
.setting-amount-box{display:inline-flex;align-items:baseline;gap:3px;padding:4px 10px;background:var(--bg);
border:1px solid var(--line);border-radius:8px;font-size:1.4rem;font-weight:700}
.setting-amount-box:focus-within{outline:2px solid var(--accent);outline-offset:2px}
.setting-amount input{appearance:textfield;width:6ch;padding:0;color:var(--ink);background:transparent;
border:0;outline:0;font:inherit;font-size:1.9rem;font-variant-numeric:tabular-nums}
.setting-amount input::-webkit-inner-spin-button,.setting-amount input::-webkit-outer-spin-button{appearance:none;margin:0}
.setting-go button{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);
color:var(--ink);font:inherit;font-weight:600;cursor:pointer}
.setting-go :is([data-setting-go],[data-setting-save]){background:var(--filter-selected-bg);color:var(--filter-selected-ink);
border-color:var(--filter-selected-bg);--button-hover-tint:rgb(255 255 255 / 10%)}
.setting-go button:disabled{cursor:default;opacity:.6}
.setting-confirm{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:10px;color:var(--ink-2)}
.setting-confirm[hidden]{display:none}
.setting-error{margin:8px 0 0;color:var(--error);overflow-wrap:anywhere}
.setting-error:empty{margin:0}
'''

SCRIPT = r'''
const pageSettingControls=(()=>{
 function connectSlider(root){
  const input=root.querySelector('input');
  const output=root.querySelector('[data-slider-value]');
  const now=root.querySelector('.setting-slider-now');
  const min=Number(input.min),max=Number(input.max),decimals=Number(root.dataset.decimals);
  let live=Number(root.dataset.live);
  const format=value=>Number(value).toFixed(decimals);
  const position=value=>Math.min(100,Math.max(0,(value-min)/(max-min)*100))+'%';
  function draw(){
   const value=input.valueAsNumber;
   output.value=format(value);
   input.setAttribute('aria-valuetext',output.value);
   root.style.setProperty('--setting-fill',position(value));
  }
  input.addEventListener('input',draw);
  draw();
  return {
   input,format,
   get value(){return input.valueAsNumber;},
   get live(){return live;},
   setValue(value){input.valueAsNumber=value;draw();},
   setLive(value,note=''){
    live=Number(value);
    now.textContent='Now '+format(live)+(note ? ', '+note : '');
    root.style.setProperty('--setting-live',position(live));
   }
  };
 }

 function connectGo(root,slider,amount,describe){
  const go=root.querySelector('[data-setting-go]');
  const line=root.querySelector('[data-setting-confirm]');
  const summary=root.querySelector('[data-setting-summary]');
  const confirm=root.querySelector('[data-setting-save]');
  const cancel=root.querySelector('[data-setting-cancel]');
  const error=root.querySelector('[data-setting-error]');
  const currency=new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',signDisplay:'exceptZero'});
  let chosen, saving=false;
  function close(){
   line.hidden=true;
   go.setAttribute('aria-expanded','false');
  }
  function cancelConfirmation(){
   if(!saving && !line.hidden){
    const moveFocus=line.contains(document.activeElement);
    close();
    if(moveFocus) go.focus();
   }
  }
  go.addEventListener('click',()=>{
   const {newValue,monthlyChange,liveNote}=describe();
   chosen={value:slider.input.value,live:slider.live,amount:amount.value,liveNote};
   summary.textContent='Change from '+slider.format(chosen.live)+' to '+newValue+'. The estimate changes by '+
    currency.format(monthlyChange)+' a month.';
   error.textContent='';
   line.hidden=false;
   go.setAttribute('aria-expanded','true');
   confirm.focus();
  });
  cancel.addEventListener('click',cancelConfirmation);
  line.addEventListener('keydown',event=>{
   if(event.key==='Escape'){event.preventDefault();cancelConfirmation();}
  });
  slider.input.addEventListener('input',cancelConfirmation);
  amount.addEventListener('input',cancelConfirmation);
  confirm.addEventListener('click',async()=>{
   if(saving || line.hidden) return;
   if(chosen.value!==slider.input.value || chosen.live!==slider.live || chosen.amount!==amount.value){
    cancelConfirmation();
    return;
   }
   saving=true;
   const controls=[go,confirm,cancel,slider.input,amount];
   const disabled=controls.map(control=>control.disabled);
   controls.forEach(control=>control.disabled=true);
   let saved=false;
   try{
    const tokenResponse=await fetch('/console/tune',{cache:'no-store'});
    const {token,reason}=await tokenResponse.json();
    if(!tokenResponse.ok || typeof token!=='string' || !token) throw new Error(reason || 'The Go token is not available.');
    const response=await fetch('/console/tune',{
     method:'POST',
     body:new URLSearchParams({page:root.dataset.page,key:root.dataset.key,value:chosen.value,token})
    });
    const result=await response.json();
    const live=typeof result.live_value==='string' && result.live_value.trim() ? Number(result.live_value) : NaN;
    if(Number.isFinite(live) && live!==slider.live){
     slider.setLive(live,live===Number(chosen.value) ? chosen.liveNote : '');
    }
    if(!response.ok || result.state!=='saved'){
     throw new Error(typeof result.reason==='string' ? result.reason : '');
    }
    saved=Number.isFinite(live);
    if(!saved) error.textContent='The live value is not available.';
   }catch(failure){
    error.textContent=failure instanceof Error && failure.message.trim()
     ? failure.message.trim().replace(/\s+/g,' ') : 'The setting could not be saved.';
   }finally{
    saving=false;
    const moveFocus=line.contains(document.activeElement) || document.activeElement===document.body;
    close();
    controls.forEach((control,index)=>control.disabled=disabled[index]);
    if(moveFocus) go.focus();
   }
   if(saved) root.dispatchEvent(new CustomEvent('setting-saved',{bubbles:true,detail:{value:slider.live}}));
  });
  return {setLabel(label){go.textContent=label;},cancelConfirmation};
 }
 return {connectSlider,connectGo};
})();
'''

ASSETS = f"<style>{STYLE}</style><script>{SCRIPT}</script>"

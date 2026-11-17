"""Translate the former review pages' address fields."""

SCRIPT = r'''function reviewPlace(params,legacy=''){
 legacy=params.get('legacy') || legacy;
 if(!params.has('owner') && params.has('showrunner')) params.set('owner',params.get('showrunner'));
 if(!params.has('item') && params.has('row')) params.set('item',params.get('row'));
 if(legacy==='investigation'){
  const shows={'not-read':'open','answer ready':'For you',open:'Waiting',read:'Done',all:'all'};
  params.set('show',shows[params.get('show') || 'not-read'] || 'open');
  if(params.get('owner')) params.set('source-owner',params.get('owner'));
 }
 params.delete('showrunner');params.delete('row');params.delete('legacy');
 return params;
}
'''

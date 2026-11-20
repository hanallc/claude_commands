"""Translate the former review pages' address fields."""

SHOW_ALIAS_SCRIPT = r'''function expandStateAlias(params,select=(show,state)=>show ?? state){
 if(!params.has('show') && !params.has('state')) return;
 params.set('show',select(params.get('show'),params.get('state') || ''));
 params.delete('state');
}
'''

SCRIPT = SHOW_ALIAS_SCRIPT + r'''function expandReviewAliases(params){
 expandStateAlias(params);
 if(!params.has('owner') && params.has('showrunner')) params.set('owner',params.get('showrunner'));
 if(!params.has('item') && params.has('row')) params.set('item',params.get('row'));
 params.delete('showrunner');params.delete('row');
}
const reviewPlaceOptions={expandFilters:expandReviewAliases};
function reviewPlace(params,legacy=''){
 expandReviewAliases(params);
 legacy=params.get('legacy') || legacy;
 // An old investigation link can supply its owner and Show in different sources.
 if(legacy==='investigation'){
  const shows={'not-read':'open','answer ready':'For you',open:'Waiting',read:'Done',all:'all'};
  params.set('show',shows[params.get('show') || 'not-read'] || 'open');
  if(params.get('owner') && !params.has('source-owner')) params.set('source-owner',params.get('owner'));
 }
 params.delete('legacy');
 return params;
}
'''

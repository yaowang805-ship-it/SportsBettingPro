tell application "Google Chrome"
    set result to execute active tab of front window javascript "(() => { const r={}; for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);const v=localStorage.getItem(k);r[k]=v?v.substring(0,200):\"(null)\"} return JSON.stringify(r); })()"
    return result
end tell

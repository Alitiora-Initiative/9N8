import UniversalProvider from "@walletconnect/universal-provider";
import QRCode from "qrcode";

const API="https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next";
const telegram=window.Telegram&&window.Telegram.WebApp;
const $=id=>document.getElementById(id);
const connectButton=$("connectWalletSession"),verifyButton=$("verifyWalletSession"),disconnectButton=$("disconnectWalletSession");
const sessionStatus=$("walletSessionStatus"),networkSelect=$("network");
const labels={"1":"Ethereum Mainnet","8453":"Base Mainnet","56":"BNB Smart Chain","mainnet-beta":"Solana Mainnet"};
const rpcEndpoints={1:"https://cloudflare-eth.com",8453:"https://mainnet.base.org",56:"https://bsc-dataseed.binance.org"};
let wc=null,projectId="",currentAddress="",injected=null,verified=null,busy=false,qrDialog=null;

function setStatus(message,kind="info"){if(!sessionStatus)return;sessionStatus.textContent=message;sessionStatus.dataset.kind=kind;}
function shortAddress(v){return v&&v.length>16?v.slice(0,8)+"…"+v.slice(-6):v;}
function toHex(v){return "0x"+Array.from(new TextEncoder().encode(String(v))).map(x=>x.toString(16).padStart(2,"0")).join("");}
function toBase64(v){const b=v instanceof Uint8Array?v:new Uint8Array(v);let s="";for(const x of b)s+=String.fromCharCode(x);return btoa(s);}
function chainId(v){if(typeof v==="number"&&Number.isFinite(v))return String(v);const s=String(v??"");if(/^0x[0-9a-f]+$/i.test(s))return String(parseInt(s,16));return /^\d+$/.test(s)?s:"";}
function walletProviderName(v){const s=String(v||"").toLowerCase();if(s.includes("phantom"))return "phantom";if(s.includes("trust"))return "trust_wallet";if(s.includes("metamask"))return "metamask";return "walletconnect";}
async function post(body){
 if(!telegram?.initData)throw new Error("Open 9N8 Connect from your Telegram private chat.");
 const response=await fetch(API,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({...body,initData:telegram.initData}),cache:"no-store",credentials:"omit"});
 const payload=await response.json().catch(()=>({}));if(!response.ok||payload.ok!==true)throw new Error(payload.message||payload.error||"Wallet request rejected.");return payload;
}
function ensureQrDialog(){
 if(qrDialog)return qrDialog;
 qrDialog=document.createElement("dialog");qrDialog.id="n8WalletConnectQr";
 qrDialog.setAttribute("aria-labelledby","n8WalletConnectQrTitle");
 qrDialog.innerHTML="<form method='dialog' style='display:flex;justify-content:flex-end'><button type='submit' aria-label='Close QR dialog'>Close</button></form>"+
 "<h2 id='n8WalletConnectQrTitle'>Connect with WalletConnect</h2>"+
 "<p>Scan with your wallet. 9N8 requests an ownership signature only; it does not approve a trade.</p>"+
 "<canvas id='n8WalletConnectQrCanvas' width='280' height='280'></canvas>"+
 "<p id='n8WalletConnectQrUri' style='overflow-wrap:anywhere'></p>";
 qrDialog.style.cssText="max-width:min(92vw,420px);border:1px solid #52606d;border-radius:16px;padding:24px;background:#111827;color:#f9fafb";
 document.body.appendChild(qrDialog);return qrDialog;
}
function closeQr(){if(qrDialog?.open)qrDialog.close();}
async function showQr(uri){
 const dialog=ensureQrDialog();dialog.querySelector("#n8WalletConnectQrUri").textContent=uri;
 await QRCode.toCanvas(dialog.querySelector("#n8WalletConnectQrCanvas"),uri,{width:280,margin:2,errorCorrectionLevel:"M"});
 if(!dialog.open)dialog.showModal();
}
function setConnected(session){
 currentAddress=session.address;verifyButton.disabled=false;disconnectButton.disabled=false;
 setStatus(session.walletName+" connected: "+shortAddress(session.address)+" · "+session.network+". Verify ownership to link it to 9N8.","success");
}
function evmCandidates(){
 const list=[],eth=window.ethereum;if(Array.isArray(eth?.providers))list.push(...eth.providers);if(eth)list.push(eth);
 if(window.phantom?.ethereum)list.push(window.phantom.ethereum);if(window.trustwallet?.ethereum)list.push(window.trustwallet.ethereum);
 return [...new Set(list)];
}
function injectedName(p){if(p?.isMetaMask&&!p?.isBraveWallet)return "MetaMask";if(p?.isTrust||p?.isTrustWallet)return "Trust Wallet";if(p?.isPhantom)return "Phantom";return "Injected wallet";}
async function connectSolana(){
 const provider=window.phantom?.solana||window.solana;
 if(!provider||typeof provider.connect!=="function"||typeof provider.signMessage!=="function"||typeof provider.signTransaction!=="function")throw new Error("Open 9N8 inside Phantom with message and transaction signing enabled.");
 const result=await provider.connect(),key=result?.publicKey||provider.publicKey,address=key?.toString?.()||String(key||"");
 if(!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address))throw new Error("Wallet returned an invalid Solana address.");
 injected={namespace:"solana",chainId:"mainnet-beta",address,network:labels["mainnet-beta"],walletName:"Phantom",provider,connectionType:"injected_session"};return injected;
}
async function connectInjectedEvm(expected){
 const candidates=evmCandidates(),provider=candidates.find(x=>x.isMetaMask&&!x.isBraveWallet)||candidates.find(x=>x.isTrust||x.isTrustWallet)||candidates[0];
 if(!provider||typeof provider.request!=="function")return null;
 const accounts=await provider.request({method:"eth_requestAccounts"}),address=Array.isArray(accounts)?String(accounts[0]||""):"";
 if(!/^0x[a-fA-F0-9]{40}$/.test(address))throw new Error("Wallet returned an invalid EVM address.");
 const actual=chainId(await provider.request({method:"eth_chainId"}));if(actual!==expected)throw new Error("Switch your wallet to "+labels[expected]+" and reconnect.");
 injected={namespace:"eip155",chainId:actual,address,network:labels[actual],walletName:injectedName(provider),provider,connectionType:"injected_session"};return injected;
}
function evmNamespace(id){
 const n=Number(id);return {methods:["eth_accounts","eth_chainId","personal_sign"],chains:["eip155:"+id],events:["chainChanged","accountsChanged"],rpcMap:{[n]:rpcEndpoints[n]||("https://rpc.walletconnect.org/v1/?chainId=eip155:"+id+"&projectId="+projectId)}};
}
async function initialize(){
 if(!connectButton||!verifyButton||!disconnectButton)return;
 if(!telegram?.initData){connectButton.disabled=true;setStatus("Open this page from the Connect button inside your 9N8 Telegram private chat.","warning");return;}
 connectButton.disabled=true;setStatus("Checking secure wallet configuration…");
 try{
  const response=await fetch(API+"?view=wallet-config",{cache:"no-store",credentials:"omit"}),config=await response.json();
  if(!response.ok||config.ok!==true)throw new Error("Could not read wallet connection settings.");
  projectId=String(config.walletConnectProjectId||config.reownProjectId||"").trim();
  if(projectId){
   wc=await UniversalProvider.init({logger:"error",relayUrl:"wss://relay.walletconnect.com",projectId,metadata:{name:"9N8 Trading Intelligence",description:"Ownership verification only; no trade approval.",url:location.origin,icons:[]}});
   wc.on("display_uri",uri=>showQr(uri).catch(e=>setStatus("QR rendering failed: "+e.message,"error")));
   wc.on("connect",closeQr);
   wc.on("accountsChanged",accounts=>{const raw=Array.isArray(accounts)?String(accounts[0]||""):"";currentAddress=raw.includes(":")?raw.split(":").pop():raw;verified=null;verifyButton.disabled=!currentAddress;disconnectButton.disabled=!currentAddress;setStatus(currentAddress?"WalletConnect account changed. Verify ownership again.":"WalletConnect account disconnected.","warning");});
   wc.on("chainChanged",()=>{verified=null;verifyButton.disabled=!currentAddress;setStatus("Wallet network changed. Verify ownership again.","warning");});
   wc.on("disconnect",()=>{currentAddress="";verified=null;verifyButton.disabled=true;disconnectButton.disabled=true;setStatus("WalletConnect session disconnected.","warning");});
  }
  connectButton.disabled=false;setStatus(wc?"WalletConnect QR is ready for Ethereum, Base and BNB Chain. Phantom handles Solana.":"WalletConnect is not configured; injected wallets remain available.",wc?"success":"warning");
 }catch(error){connectButton.disabled=false;wc=null;setStatus("Wallet initialization failed: "+(error?.message||"unknown error"),"error");}
}
connectButton?.addEventListener("click",async()=>{
 try{
  const selected=networkSelect?.value||"eip155:1";
  if(selected.startsWith("solana:")){setConnected(await connectSolana());return;}
  const expected=selected.split(":")[1]||"1",direct=await connectInjectedEvm(expected);
  if(direct){setConnected(direct);return;}
  if(!wc)throw new Error("No injected EVM wallet or WalletConnect project configuration was found.");
  const chain="eip155:"+expected;
  if(!wc.session?.namespaces?.eip155?.chains?.includes(chain))await wc.connect({namespaces:{eip155:evmNamespace(expected)}});
  const accounts=await wc.request({method:"eth_accounts"},chain),address=Array.isArray(accounts)?String(accounts[0]||""):"";
  if(!/^0x[a-fA-F0-9]{40}$/.test(address))throw new Error("WalletConnect returned an invalid EVM address.");
  const actual=chainId(await wc.request({method:"eth_chainId"},chain));if(actual&&actual!==expected)throw new Error("WalletConnect session is on a different chain.");
  injected=null;setConnected({address,network:labels[expected],walletName:"WalletConnect"});
 }catch(error){setStatus(error?.message||"Wallet connection was not completed.","error");}
});
verifyButton?.addEventListener("click",async()=>{
 if(busy||!currentAddress)return;busy=true;verifyButton.disabled=true;
 try{
  const selected=networkSelect?.value||"eip155:1",namespace=injected?.namespace||(selected.startsWith("solana:")?"solana":"eip155");
  const selectedChain=selected.split(":")[1],provider=injected?.provider||(namespace==="eip155"?wc:null),address=injected?.address||currentAddress;
  if(!provider||!address)throw new Error("No active wallet provider was found. Reconnect.");
  let chain=namespace==="solana"?"mainnet-beta":injected?.chainId||selectedChain;
  if(namespace==="solana"){if(!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address))throw new Error("Invalid Solana address.");}
  else{
   const active=chainId(await provider.request({method:"eth_chainId"}, "eip155:"+chain));
   if(active&&active!==chain)throw new Error("Switch to the selected EVM network and reconnect.");
   if(!/^0x[a-fA-F0-9]{40}$/.test(address))throw new Error("Invalid EVM address.");
  }
  const walletName=injected?.walletName||"WalletConnect",connectionType=injected?.connectionType||"walletconnect_session";
  setStatus("Creating one-time ownership challenge…");
  const challenge=await post({action:"wallet_challenge",provider:walletProviderName(walletName),connectionType,namespace,chainId:chain,address});
  setStatus("Approve the ownership message. This signature does not authorize a trade…");
  let signature;
  if(namespace==="eip155")signature=await provider.request({method:"personal_sign",params:[toHex(challenge.message),address]},"eip155:"+chain);
  else{if(typeof provider.signMessage!=="function")throw new Error("Solana wallet cannot sign messages.");const signed=await provider.signMessage(new TextEncoder().encode(challenge.message));signature=toBase64(signed?.signature||signed);}
  const result=await post({action:"wallet_verify",challengeId:challenge.challengeId,signature,walletName,connectionType});
  verified={walletConnectionId:result.connectionId,address,chainId:chain,namespace,network:result.network,provider,walletName,connectionType};
  window.dispatchEvent(new CustomEvent("n8-wallet-session",{detail:{walletConnectionId:result.connectionId,address,chainId:chain,namespace,network:result.network,walletName}}));
  disconnectButton.disabled=false;setStatus("Verified and linked: "+result.address+" · "+result.network+". No transaction was sent.","success");
  const status=$("walletStatus");if(status)status.textContent="Verified wallet session: "+result.address+" · "+result.network+". No wallet transaction was sent.";
 }catch(error){setStatus(error?.message||"Wallet verification failed.","error");}
 finally{busy=false;verifyButton.disabled=!currentAddress;}
});
disconnectButton?.addEventListener("click",async()=>{
 if(busy)return;busy=true;disconnectButton.disabled=true;
 try{
  const selected=networkSelect?.value||"eip155:1",namespace=injected?.namespace||(selected.startsWith("solana:")?"solana":"eip155");
  const address=injected?.address||currentAddress,chain=injected?.chainId||(namespace==="solana"?"mainnet-beta":selected.split(":")[1]),network=injected?.network||labels[chain]||"";
  if(address&&network)await post({action:"wallet_revoke",namespace,address,chainId:chain,network});
  if(injected?.provider?.disconnect){try{await injected.provider.disconnect();}catch(_){}}
  else if(wc?.session){try{await wc.disconnect();}catch(_){}}
  injected=null;verified=null;currentAddress="";closeQr();verifyButton.disabled=true;disconnectButton.disabled=true;setStatus("Wallet session disconnected and its 9N8 link revoked.","success");
 }catch(error){setStatus(error?.message||"Could not revoke wallet link.","error");disconnectButton.disabled=false;}
 finally{busy=false;}
});
networkSelect?.addEventListener("change",()=>{if(currentAddress){verified=null;verifyButton.disabled=true;setStatus("Network changed. Reconnect and verify on the selected network.","warning");}});
window.n8GetSolanaWalletSession=()=>{
 if(!verified||verified.namespace!=="solana"||verified.chainId!=="mainnet-beta"||networkSelect?.value!=="solana:mainnet-beta")return null;
 const provider=injected?.namespace==="solana"?injected.provider:null;
 if(injected?.address!==verified.address||!provider||typeof provider.signTransaction!=="function")return null;
 return {...verified,provider,signTransaction:transaction=>provider.signTransaction(transaction)};
};
initialize();

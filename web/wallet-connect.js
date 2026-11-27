import { EthereumProvider } from "@walletconnect/ethereum-provider";

const API = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next";
const telegram = window.Telegram && window.Telegram.WebApp;
const $ = (id) => document.getElementById(id);
const connectButton = $("connectWalletSession");
const verifyButton = $("verifyWalletSession");
const disconnectButton = $("disconnectWalletSession");
const sessionStatus = $("walletSessionStatus");
const networkSelect = $("network");
const networkLabels = {"1":"Ethereum Mainnet","8453":"Base Mainnet","56":"BNB Smart Chain","mainnet-beta":"Solana Mainnet"};
const rpcMap = {1:"https://cloudflare-eth.com",8453:"https://mainnet.base.org",56:"https://bsc-dataseed.binance.org"};

let walletConnectProvider = null;
let currentAddress = "";
let injectedSession = null;
let verifiedWalletSession = null;
let connectionBusy = false;

function setStatus(message,kind="info"){if(!sessionStatus)return;sessionStatus.textContent=message;sessionStatus.dataset.kind=kind;}
function shortAddress(value){return value&&value.length>16?value.slice(0,8)+"…"+value.slice(-6):value;}
function toHex(value){return "0x"+Array.from(new TextEncoder().encode(String(value))).map(x=>x.toString(16).padStart(2,"0")).join("");}
function toBase64(value){const bytes=value instanceof Uint8Array?value:new Uint8Array(value);let binary="";for(const byte of bytes)binary+=String.fromCharCode(byte);return btoa(binary);}
function chainIdOf(value){if(typeof value==="number"&&Number.isFinite(value))return String(value);const text=String(value??"");if(/^0x[0-9a-f]+$/i.test(text))return String(parseInt(text,16));return /^\d+$/.test(text)?text:"";}
function mapWalletProvider(name){const value=String(name||"").toLowerCase();if(value.includes("phantom"))return "phantom";if(value.includes("trust"))return "trust_wallet";if(value.includes("metamask"))return "metamask";return "walletconnect";}
async function post(body){
  if(!telegram?.initData)throw new Error("Open 9N8 Connect from your Telegram private chat.");
  const response=await fetch(API,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({...body,initData:telegram.initData}),cache:"no-store",credentials:"omit"});
  const payload=await response.json().catch(()=>({}));
  if(!response.ok||payload.ok!==true)throw new Error(payload.message||payload.error||"Wallet request rejected.");
  return payload;
}
function setConnected(session){
  currentAddress=session.address;
  verifyButton.disabled=false;
  disconnectButton.disabled=false;
  setStatus(session.walletName+" connected: "+shortAddress(session.address)+" · "+session.network+". Verify ownership to link it to 9N8.","success");
}
function injectedEvmCandidates(){
  const result=[];const ethereum=window.ethereum;
  if(Array.isArray(ethereum?.providers))result.push(...ethereum.providers);
  if(ethereum)result.push(ethereum);
  if(window.phantom?.ethereum)result.push(window.phantom.ethereum);
  if(window.trustwallet?.ethereum)result.push(window.trustwallet.ethereum);
  return [...new Set(result)];
}
function injectedWalletName(provider){
  if(provider?.isMetaMask&&!provider?.isBraveWallet)return "MetaMask";
  if(provider?.isTrust||provider?.isTrustWallet)return "Trust Wallet";
  if(provider?.isPhantom)return "Phantom";
  return "Injected wallet";
}
async function connectSolana(){
  const provider=window.phantom?.solana||window.solana;
  if(!provider||typeof provider.connect!=="function"||typeof provider.signMessage!=="function"||typeof provider.signTransaction!=="function")
    throw new Error("Open 9N8 inside Phantom. A Solana injected wallet is required for transaction signing.");
  const result=await provider.connect();
  const key=result?.publicKey||provider.publicKey;
  const address=key?.toString?.()||String(key||"");
  if(!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address))throw new Error("Wallet returned an invalid Solana address.");
  return injectedSession={namespace:"solana",chainId:"mainnet-beta",address,network:"Solana Mainnet",walletName:"Phantom",provider,connectionType:"injected_session"};
}
async function connectInjectedEvm(expected){
  const candidates=injectedEvmCandidates();
  const provider=candidates.find(x=>x.isMetaMask&&!x.isBraveWallet)||candidates.find(x=>x.isTrust||x.isTrustWallet)||candidates[0];
  if(!provider||typeof provider.request!=="function")return null;
  const accounts=await provider.request({method:"eth_requestAccounts"});
  const address=Array.isArray(accounts)?String(accounts[0]||""):"";
  if(!/^0x[a-fA-F0-9]{40}$/.test(address))throw new Error("Wallet returned an invalid EVM address.");
  const chainId=chainIdOf(await provider.request({method:"eth_chainId"}));
  if(chainId!==expected)throw new Error("Switch your wallet to "+networkLabels[expected]+" and reconnect.");
  return injectedSession={namespace:"eip155",chainId,address,network:networkLabels[chainId],walletName:injectedWalletName(provider),provider,connectionType:"injected_session"};
}
async function initialize(){
  if(!connectButton||!verifyButton||!disconnectButton)return;
  if(!telegram?.initData){connectButton.disabled=true;setStatus("Open this page from the Connect button inside your 9N8 Telegram private chat.","warning");return;}
  connectButton.disabled=true;setStatus("Checking secure wallet connection configuration…");
  try{
    const response=await fetch(API+"?view=wallet-config",{cache:"no-store",credentials:"omit"});
    const config=await response.json();
    if(!response.ok||config.ok!==true)throw new Error("Could not read wallet connection settings.");
    const projectId=String(config.walletConnectProjectId||config.reownProjectId||"").trim();
    if(projectId){
      walletConnectProvider=await EthereumProvider.init({
        projectId,optionalChains:[1,8453,56],showQrModal:true,
        methods:["eth_accounts","eth_chainId","personal_sign"],
        events:["accountsChanged","chainChanged","disconnect"],rpcMap,
        metadata:{name:"9N8 Trading Intelligence",description:"Wallet ownership verification. No transaction approval is granted.",url:location.origin,icons:[]},
      });
      walletConnectProvider.on("accountsChanged",accounts=>{
        currentAddress=Array.isArray(accounts)?String(accounts[0]||""):"";
        verifiedWalletSession=null;verifyButton.disabled=!currentAddress;disconnectButton.disabled=!currentAddress;
        setStatus(currentAddress?"WalletConnect account changed. Verify ownership again.":"WalletConnect account disconnected.","warning");
      });
      walletConnectProvider.on("chainChanged",()=>{
        verifiedWalletSession=null;verifyButton.disabled=!currentAddress;
        setStatus("Wallet network changed. Verify ownership again before using this session.","warning");
      });
      walletConnectProvider.on("disconnect",()=>{
        currentAddress="";verifiedWalletSession=null;verifyButton.disabled=true;disconnectButton.disabled=true;
        setStatus("WalletConnect session disconnected.","warning");
      });
    }
    connectButton.disabled=false;
    setStatus(walletConnectProvider?"WalletConnect QR is ready for Ethereum, Base and BNB Chain. Phantom is used for Solana.":"WalletConnect is not configured. Supported injected wallets can still connect.",walletConnectProvider?"success":"warning");
  }catch(error){connectButton.disabled=false;walletConnectProvider=null;setStatus("Wallet initialization failed: "+(error?.message||"unknown error"),"error");}
}
connectButton?.addEventListener("click",async()=>{
  try{
    const selected=networkSelect?.value||"eip155:1";
    if(selected.startsWith("solana:")){setConnected(await connectSolana());return;}
    const expected=selected.split(":")[1]||"1";
    const injected=await connectInjectedEvm(expected);
    if(injected){setConnected(injected);return;}
    if(!walletConnectProvider)throw new Error("No injected EVM wallet or WalletConnect project configuration was found.");
    if(!walletConnectProvider.session)await walletConnectProvider.connect({optionalChains:[Number(expected)]});
    const accounts=await walletConnectProvider.request({method:"eth_accounts"});
    const address=Array.isArray(accounts)?String(accounts[0]||""):"";
    if(!/^0x[a-fA-F0-9]{40}$/.test(address))throw new Error("WalletConnect did not return a valid EVM address.");
    const actual=chainIdOf(await walletConnectProvider.request({method:"eth_chainId"}));
    if(actual!==expected)throw new Error("WalletConnect session is on chain "+actual+"; select or switch to "+networkLabels[expected]+".");
    injectedSession=null;
    setConnected({address,network:networkLabels[expected],walletName:"WalletConnect"});
  }catch(error){setStatus(error?.message||"Wallet connection was not completed.","error");}
});
verifyButton?.addEventListener("click",async()=>{
  if(connectionBusy||!currentAddress)return;
  connectionBusy=true;verifyButton.disabled=true;
  try{
    const selected=networkSelect?.value||"eip155:1";
    const namespace=injectedSession?.namespace||(selected.startsWith("solana:")?"solana":"eip155");
    const provider=injectedSession?.provider||(namespace==="eip155"?walletConnectProvider:null);
    const address=injectedSession?.address||currentAddress;
    if(!provider||!address)throw new Error("No active wallet provider was found. Reconnect your wallet.");
    let chainId="";
    if(namespace==="solana"){chainId="mainnet-beta";if(!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address))throw new Error("Invalid Solana address.");}
    else{
      chainId=injectedSession?.chainId||chainIdOf(await provider.request({method:"eth_chainId"}));
      const expected=selected.split(":")[1];
      if(!chainId||chainId!==expected)throw new Error("Switch to the selected EVM network and reconnect.");
      if(!/^0x[a-fA-F0-9]{40}$/.test(address))throw new Error("Invalid EVM address.");
    }
    const walletName=injectedSession?.walletName||"WalletConnect";
    const connectionType=injectedSession?.connectionType||"walletconnect_session";
    setStatus("Creating one-time ownership challenge…");
    const challenge=await post({action:"wallet_challenge",provider:mapWalletProvider(walletName),connectionType,namespace,chainId,address});
    setStatus("Approve the ownership message in your wallet. This does not authorize a transaction…");
    let signature;
    if(namespace==="eip155")signature=await provider.request({method:"personal_sign",params:[toHex(challenge.message),address]});
    else{
      if(typeof provider.signMessage!=="function")throw new Error("The Solana wallet cannot sign messages.");
      const signed=await provider.signMessage(new TextEncoder().encode(challenge.message));
      signature=toBase64(signed?.signature||signed);
    }
    const result=await post({action:"wallet_verify",challengeId:challenge.challengeId,signature,walletName,connectionType});
    verifiedWalletSession={walletConnectionId:result.connectionId,address,chainId,namespace,network:result.network,provider,walletName,connectionType};
    window.dispatchEvent(new CustomEvent("n8-wallet-session",{detail:{walletConnectionId:result.connectionId,address,chainId,namespace,network:result.network,walletName}}));
    disconnectButton.disabled=false;
    setStatus("Verified and linked: "+result.address+" · "+result.network+". This signature only proves ownership; no transaction was sent.","success");
    const status=$("walletStatus");if(status)status.textContent="Verified wallet session: "+result.address+" · "+result.network+". No wallet transaction was sent.";
  }catch(error){setStatus(error?.message||"Wallet ownership verification failed.","error");}
  finally{connectionBusy=false;verifyButton.disabled=!currentAddress;}
});
disconnectButton?.addEventListener("click",async()=>{
  if(connectionBusy)return;
  connectionBusy=true;disconnectButton.disabled=true;
  try{
    const selected=networkSelect?.value||"eip155:1";
    const namespace=injectedSession?.namespace||(selected.startsWith("solana:")?"solana":"eip155");
    const address=injectedSession?.address||currentAddress;
    const chainId=injectedSession?.chainId||(namespace==="solana"?"mainnet-beta":chainIdOf(await walletConnectProvider?.request({method:"eth_chainId"})));
    const network=injectedSession?.network||networkLabels[chainId]||"";
    if(address&&network)await post({action:"wallet_revoke",namespace,address,chainId,network});
    if(injectedSession?.provider?.disconnect){try{await injectedSession.provider.disconnect();}catch(_){}}
    else if(walletConnectProvider?.session){try{await walletConnectProvider.disconnect();}catch(_){}}
    injectedSession=null;verifiedWalletSession=null;currentAddress="";
    verifyButton.disabled=true;disconnectButton.disabled=true;
    setStatus("Wallet session disconnected and its 9N8 link revoked.","success");
  }catch(error){setStatus(error?.message||"Could not revoke the wallet link.","error");disconnectButton.disabled=false;}
  finally{connectionBusy=false;}
});
networkSelect?.addEventListener("change",()=>{
  if(currentAddress){verifiedWalletSession=null;verifyButton.disabled=true;setStatus("Network changed. Reconnect and verify on the selected network.","warning");}
});
window.n8GetSolanaWalletSession=()=>{
  if(!verifiedWalletSession||verifiedWalletSession.namespace!=="solana"||verifiedWalletSession.chainId!=="mainnet-beta"||networkSelect?.value!=="solana:mainnet-beta")return null;
  const provider=injectedSession?.namespace==="solana"?injectedSession.provider:null;
  if(injectedSession?.address!==verifiedWalletSession.address||!provider||typeof provider.signTransaction!=="function")return null;
  return {...verifiedWalletSession,provider,signTransaction:transaction=>provider.signTransaction(transaction)};
};
initialize();

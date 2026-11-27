// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;
/// @title 9N8RiskPolicy
/// @notice Optional EVM policy registry. Not deployed or used by the Solana signer.
contract N8RiskPolicy {
 address public immutable owner;
 bool public halted;
 uint16 public maxPositionBps=200;
 mapping(address=>bool) public blockedToken;
 event HaltChanged(bool halted);
 event TokenPolicyChanged(address indexed token,bool blocked);
 event MaxPositionChanged(uint16 bps);
 error Unauthorized(); error TradingHalted(); error TokenBlocked(); error InvalidRiskLimit();
 constructor(){owner=msg.sender;}
 modifier onlyOwner(){if(msg.sender!=owner)revert Unauthorized();_;}
 function setHalted(bool value)external onlyOwner{halted=value;emit HaltChanged(value);}
 function setBlockedToken(address token,bool blocked)external onlyOwner{if(token==address(0))revert InvalidRiskLimit();blockedToken[token]=blocked;emit TokenPolicyChanged(token,blocked);}
 function setMaxPositionBps(uint16 value)external onlyOwner{if(value==0||value>200)revert InvalidRiskLimit();maxPositionBps=value;emit MaxPositionChanged(value);}
 function check(address token,uint16 positionBps)external view returns(bool){if(halted)revert TradingHalted();if(blockedToken[token])revert TokenBlocked();if(positionBps==0||positionBps>maxPositionBps)revert InvalidRiskLimit();return true;}
}

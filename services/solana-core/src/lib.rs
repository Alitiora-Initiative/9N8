use std::collections::VecDeque;
#[derive(Debug,Clone,Copy,PartialEq)]
pub struct AmmQuote{pub amount_out:u128,pub price_impact_bps:u128}
pub fn constant_product_quote(amount_in:u128,reserve_in:u128,reserve_out:u128,fee_bps:u128)->Option<AmmQuote>{
 if amount_in==0||reserve_in==0||reserve_out==0||fee_bps>=10_000{return None;}
 let effective=amount_in.checked_mul(10_000-fee_bps)?/10_000;if effective==0{return None;}
 let amount_out=effective.checked_mul(reserve_out)?/reserve_in.checked_add(effective)?;
 if amount_out==0||amount_out>=reserve_out{return None;}
 let spot=amount_in.checked_mul(reserve_out)?/reserve_in;
 let impact=if spot==0{0}else{spot.saturating_sub(amount_out).saturating_mul(10_000)/spot};
 Some(AmmQuote{amount_out,price_impact_bps:impact})
}
#[derive(Debug)]
pub struct SignatureWindow{capacity:usize,values:VecDeque<String>}
impl SignatureWindow{
 pub fn new(capacity:usize)->Self{Self{capacity:capacity.max(1),values:VecDeque::new()}}
 pub fn insert_once(&mut self,signature:impl Into<String>)->bool{
  let value=signature.into();if self.values.iter().any(|v|v==&value){return false;}
  self.values.push_back(value);while self.values.len()>self.capacity{self.values.pop_front();}true
 }
 pub fn len(&self)->usize{self.values.len()}
}
#[cfg(test)]
mod tests{
 use super::*;
 #[test]fn amm_math_is_integer_safe(){let q=constant_product_quote(1_000_000,100_000_000,200_000_000,30).unwrap();assert!(q.amount_out>0&&q.amount_out<2_000_000);assert!(q.price_impact_bps>0);assert!(constant_product_quote(1,1,1,10_000).is_none());}
 #[test]fn signature_window_is_bounded(){let mut w=SignatureWindow::new(2);assert!(w.insert_once("a"));assert!(!w.insert_once("a"));assert!(w.insert_once("b"));assert!(w.insert_once("c"));assert_eq!(w.len(),2);assert!(w.insert_once("a"));}
}

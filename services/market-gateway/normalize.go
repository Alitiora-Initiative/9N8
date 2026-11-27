package main

import (
 "errors"
 "fmt"
 "math"
 "sort"
 "strconv"
 "time"
)

type RawCandle struct {
 Timestamp string `json:"timestamp"`
 Open string `json:"open"`
 High string `json:"high"`
 Low string `json:"low"`
 Close string `json:"close"`
 Volume string `json:"volume"`
}
type Candle struct {
 Timestamp time.Time `json:"timestamp"`
 Open float64 `json:"open"`
 High float64 `json:"high"`
 Low float64 `json:"low"`
 Close float64 `json:"close"`
 Volume float64 `json:"volume"`
}
func parseNumber(value,field string)(float64,error){
 n,err:=strconv.ParseFloat(value,64)
 if err!=nil||math.IsNaN(n)||math.IsInf(n,0)||n<0||(field!="volume"&&n==0){return 0,fmt.Errorf("%s must be finite and valid",field)}
 return n,nil
}
func NormalizeOHLCV(rows []RawCandle,interval time.Duration,now time.Time)([]Candle,error){
 if interval<=0||now.IsZero(){return nil,errors.New("interval and current time are required")}
 seen:=make(map[int64]struct{},len(rows));out:=make([]Candle,0,len(rows))
 for _,row:=range rows{
  stamp,err:=time.Parse(time.RFC3339Nano,row.Timestamp);if err!=nil{return nil,fmt.Errorf("invalid RFC3339 timestamp: %w",err)}
  stamp=stamp.UTC();if stamp.After(now.UTC()){return nil,errors.New("future-dated candle rejected")}
  if _,ok:=seen[stamp.UnixNano()];ok{return nil,errors.New("duplicate candle timestamp rejected")};seen[stamp.UnixNano()]=struct{}{}
  if stamp.Add(interval).After(now.UTC()){continue}
  o,e:=parseNumber(row.Open,"open");if e!=nil{return nil,e}
  h,e:=parseNumber(row.High,"high");if e!=nil{return nil,e}
  l,e:=parseNumber(row.Low,"low");if e!=nil{return nil,e}
  c,e:=parseNumber(row.Close,"close");if e!=nil{return nil,e}
  v,e:=parseNumber(row.Volume,"volume");if e!=nil{return nil,e}
  if h<o||h<c||h<l||l>o||l>c{return nil,errors.New("invalid OHLC relationship")}
  out=append(out,Candle{Timestamp:stamp,Open:o,High:h,Low:l,Close:c,Volume:v})
 }
 sort.Slice(out,func(i,j int)bool{return out[i].Timestamp.Before(out[j].Timestamp)})
 return out,nil
}

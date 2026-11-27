package main
import("testing";"time")
func TestNormalizeSortsClosedBars(t *testing.T){
 now:=time.Date(2026,11,27,12,10,0,0,time.UTC)
 rows:=[]RawCandle{
  {Timestamp:"2026-11-27T12:05:00Z",Open:"2",High:"2.2",Low:"1.9",Close:"2.1",Volume:"20"},
  {Timestamp:"2026-11-27T12:00:00Z",Open:"1",High:"1.2",Low:"0.9",Close:"1.1",Volume:"10"},
 }
 got,err:=NormalizeOHLCV(rows,5*time.Minute,now)
 if err!=nil{t.Fatal(err)}
 if len(got)!=2||got[0].Open!=1||got[1].Open!=2{t.Fatalf("unexpected bars: %#v",got)}
}
func TestNormalizeRejectsDuplicateAndFuture(t *testing.T){
 now:=time.Date(2026,11,27,12,10,0,0,time.UTC)
 row:=RawCandle{Timestamp:"2026-11-27T12:00:00Z",Open:"1",High:"1.2",Low:"0.9",Close:"1.1",Volume:"10"}
 if _,err:=NormalizeOHLCV([]RawCandle{row,row},5*time.Minute,now);err==nil{t.Fatal("expected duplicate rejection")}
 row.Timestamp="2026-11-27T12:11:00Z"
 if _,err:=NormalizeOHLCV([]RawCandle{row},5*time.Minute,now);err==nil{t.Fatal("expected future rejection")}
}

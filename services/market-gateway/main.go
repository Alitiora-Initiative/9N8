package main

import (
 "encoding/json"
 "log"
 "net/http"
 "os"
 "time"
)

type normalizeRequest struct { IntervalSeconds int `json:"interval_seconds"`; Candles []RawCandle `json:"candles"` }
func main(){
 port:=os.Getenv("PORT");if port==""{port="8091"}
 token:=os.Getenv("N9N8_GATEWAY_TOKEN")
 mux:=http.NewServeMux()
 mux.HandleFunc("GET /healthz",func(w http.ResponseWriter,_ *http.Request){
  w.Header().Set("Content-Type","application/json")
  _=json.NewEncoder(w).Encode(map[string]string{"service":"9n8-market-gateway","status":"ok"})
 })
 mux.HandleFunc("POST /v1/ohlcv/normalize",func(w http.ResponseWriter,r *http.Request){
  if token==""||r.Header.Get("Authorization")!="Bearer "+token{http.Error(w,"unauthorized",http.StatusUnauthorized);return}
  r.Body=http.MaxBytesReader(w,r.Body,2<<20)
  var req normalizeRequest
  if err:=json.NewDecoder(r.Body).Decode(&req);err!=nil{http.Error(w,"invalid JSON",http.StatusBadRequest);return}
  if req.IntervalSeconds<=0||req.IntervalSeconds>31536000{http.Error(w,"invalid interval_seconds",http.StatusBadRequest);return}
  rows,err:=NormalizeOHLCV(req.Candles,time.Duration(req.IntervalSeconds)*time.Second,time.Now().UTC())
  if err!=nil{http.Error(w,err.Error(),http.StatusUnprocessableEntity);return}
  w.Header().Set("Content-Type","application/json")
  _=json.NewEncoder(w).Encode(map[string]any{"candles":rows,"count":len(rows),"timezone":"UTC"})
 })
 log.Printf("9N8 market gateway listening on :%s",port)
 log.Fatal(http.ListenAndServe(":"+port,mux))
}

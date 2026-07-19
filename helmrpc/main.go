// Command helmrpc serves Helm rendering over a gRPC connection carried on
// its own stdin/stdout, wire-compatible with grpclib-transports'
// StdioChannel (raw HTTP/2 frames, no handshake, no socket).
package main

import (
	"context"
	"log"
	"os"

	"github.com/lillecarl/easykubenix/helmrpc/pb"
	"google.golang.org/grpc"
)

type helmServer struct {
	pb.UnimplementedHelmServer
}

func (s *helmServer) Ping(ctx context.Context, req *pb.PingRequest) (*pb.PingResponse, error) {
	return &pb.PingResponse{Message: req.GetMessage()}, nil
}

func main() {
	// Reserve the real stdout exclusively for H2 frames; redirect anything
	// else (log output, accidental prints, panics writing to stdout) to
	// stderr, mirroring grpclib-transports' serve_stdio on the Python side.
	realStdout := os.Stdout
	os.Stdout = os.Stderr
	log.SetOutput(os.Stderr)

	conn := newStdioConn(os.Stdin, realStdout)
	listener := newStdioListener(conn)
	server := grpc.NewServer()
	pb.RegisterHelmServer(server, &helmServer{})

	// grpc-go's Serve loop keeps calling Accept expecting further
	// connections; a stdio pipe pair only ever has the one, so stop the
	// server once it closes (e.g. the parent closed our stdin) instead of
	// hanging forever waiting for a second connection that will never come.
	go func() {
		<-conn.closed
		server.GracefulStop()
	}()

	if err := server.Serve(listener); err != nil {
		log.Fatalf("helmrpc: serve failed: %v", err)
	}
}

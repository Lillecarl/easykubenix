package main

import (
	"io"
	"net"
	"os"
	"sync"
	"time"
)

// stdioAddr is a placeholder net.Addr for a stdio-backed connection, which
// has no real network address.
type stdioAddr struct{}

func (stdioAddr) Network() string { return "stdio" }
func (stdioAddr) String() string  { return "stdio" }

// stdioConn adapts a pair of *os.File (typically os.Stdin/os.Stdout) into a
// net.Conn so a standard google.golang.org/grpc server can serve raw HTTP/2
// frames over a subprocess's stdio pipe pair. This is wire-compatible with
// grpclib-transports' StdioChannel, which does the same thing on the Python
// side: no handshake, no socket, just H2 bytes on stdin/stdout.
type stdioConn struct {
	in  *os.File
	out *os.File

	closeOnce sync.Once
	closed    chan struct{}
}

func newStdioConn(in, out *os.File) *stdioConn {
	return &stdioConn{in: in, out: out, closed: make(chan struct{})}
}

func (c *stdioConn) Read(p []byte) (int, error)  { return c.in.Read(p) }
func (c *stdioConn) Write(p []byte) (int, error) { return c.out.Write(p) }

// Close signals that the H2 transport is done with this connection. It does
// not close the underlying files: os.Stdin/os.Stdout belong to the process,
// not to this single logical connection.
func (c *stdioConn) Close() error {
	c.closeOnce.Do(func() { close(c.closed) })
	return nil
}

func (c *stdioConn) LocalAddr() net.Addr  { return stdioAddr{} }
func (c *stdioConn) RemoteAddr() net.Addr { return stdioAddr{} }

// Deadlines are not meaningful for a pipe pair backed by os.File; grpc-go
// does not rely on them for its own liveness handling (it uses HTTP/2
// keepalive frames instead), so these are no-ops.
func (c *stdioConn) SetDeadline(t time.Time) error      { return nil }
func (c *stdioConn) SetReadDeadline(t time.Time) error  { return nil }
func (c *stdioConn) SetWriteDeadline(t time.Time) error { return nil }

// stdioListener yields exactly one connection: the process's own stdio pipe
// pair. Once that connection is accepted, further Accept calls block until
// Close is called, matching how a real listener behaves once its single
// client has been served.
type stdioListener struct {
	conn *stdioConn

	acceptOnce sync.Once
	accepted   chan net.Conn
	closeOnce  sync.Once
	closed     chan struct{}
}

func newStdioListener(conn *stdioConn) *stdioListener {
	l := &stdioListener{
		conn:     conn,
		accepted: make(chan net.Conn, 1),
		closed:   make(chan struct{}),
	}
	l.accepted <- conn
	return l
}

func (l *stdioListener) Accept() (net.Conn, error) {
	select {
	case conn, ok := <-l.accepted:
		if !ok {
			return nil, io.EOF
		}
		return conn, nil
	case <-l.closed:
		return nil, io.EOF
	}
}

func (l *stdioListener) Close() error {
	l.closeOnce.Do(func() { close(l.closed) })
	return nil
}

func (l *stdioListener) Addr() net.Addr { return stdioAddr{} }

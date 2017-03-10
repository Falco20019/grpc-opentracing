"""Implementation of the server-side open-tracing interceptor."""

import sys
import logging
import re

from grpc_opentracing import grpcext
from grpc_opentracing import OpenTracingServicerContext
import opentracing


class _OpenTracingServicerContext(OpenTracingServicerContext):

  def __init__(self, servicer_context, active_span):
    self._servicer_context = servicer_context
    self._active_span = active_span

  def is_active(self, *args, **kwargs):
    return self._servicer_context.is_active(*args, **kwargs)

  def time_remaining(self, *args, **kwargs):
    return self._servicer_context.time_remaining(*args, **kwargs)

  def cancel(self, *args, **kwargs):
    return self._servicer_context.cancel(*args, **kwargs)

  def add_callback(self, *args, **kwargs):
    return self._servicer_context.add_callback(*args, **kwargs)

  def invocation_metadata(self, *args, **kwargs):
    return self._servicer_context.invocation_metadata(*args, **kwargs)

  def peer(self, *args, **kwargs):
    return self._servicer_context.peer(*args, **kwargs)

  def send_initial_metadata(self, *args, **kwargs):
    return self._servicer_context.send_initial_metadata(*args, **kwargs)

  def set_trailing_metadata(self, *args, **kwargs):
    return self._servicer_context.set_trailing_metadata(*args, **kwargs)

  def set_code(self, *args, **kwargs):
    return self._servicer_context.set_code(*args, **kwargs)

  def set_details(self, *args, **kwargs):
    return self._servicer_context.set_details(*args, **kwargs)

  def get_active_span(self):
    return self._active_span


def _add_peer_tags(peer_str, tags):
  ipv4_re = r"ipv4:(?P<address>.+):(?P<port>\d+)"
  match = re.match(ipv4_re, peer_str)
  if match:
    tags['peer.ipv4'] = match.group('address')
    tags['peer.port'] = match.group('port')
    return
  ipv6_re = r"ipv6:\[(?P<address>.+)\]:(?P<port>\d+)"
  match = re.match(ipv6_re, peer_str)
  if match:
    tags['peer.ipv6'] = match.group('address')
    tags['peer.port'] = match.group('port')
    return
  logging.warning('unrecognized peer: %s', peer_str)


def _start_server_span(tracer, servicer_context, method):
  span_context = None
  error = None
  metadata = servicer_context.invocation_metadata()
  try:
    if metadata:
      span_context = tracer.extract(opentracing.Format.HTTP_HEADERS,
                                    dict(metadata))
  except (opentracing.UnsupportedFormatException,
          opentracing.InvalidCarrierException,
          opentracing.SpanContextCorruptedException) as e:
    logging.exception('tracer.extract() failed')
    error = e
  tags = {'component': 'grpc', 'span.kind': 'server'}
  _add_peer_tags(servicer_context.peer(), tags)
  span = tracer.start_span(
      operation_name=method, child_of=span_context, tags=tags)
  if error is not None:
    span.log_kv({'event': 'error', 'error.object': error})
  return span


class OpenTracingServerInterceptor(grpcext.UnaryServerInterceptor,
                                   grpcext.StreamServerInterceptor):

  def __init__(self, tracer, log_payloads):
    self._tracer = tracer
    self._log_payloads = log_payloads

  def intercept_unary(self, request, servicer_context, server_info, handler):
    with _start_server_span(self._tracer, servicer_context,
                            server_info.full_method) as span:
      response = None
      if self._log_payloads:
        span.log_kv({'request': request})
      try:
        response = handler(request,
                           _OpenTracingServicerContext(servicer_context, span))
      except:
        e = sys.exc_info()[0]
        span.set_tag('error', True)
        span.log_kv({'event': 'error', 'error.object': e})
        raise
      if self._log_payloads:
        span.log_kv({'response': response})
      return response

  # For RPCs that stream responses, the result can be a generator. To record
  # the span across the generated responses and detect any errors, we wrap the
  # result in a new generator that yields the response values.
  def _intercept_server_stream(self, servicer_context, server_info, handler):
    with _start_server_span(self._tracer, servicer_context,
                            server_info.full_method) as span:
      try:
        result = handler(_OpenTracingServicerContext(servicer_context, span))
        for response in result:
          yield response
      except:
        e = sys.exc_info()[0]
        span.set_tag('error', True)
        span.log_kv({'event': 'error', 'error.object': e})
        raise

  def intercept_stream(self, servicer_context, server_info, handler):
    if server_info.is_server_stream:
      return self._intercept_server_stream(servicer_context, server_info,
                                           handler)
    with _start_server_span(self._tracer, servicer_context,
                            server_info.full_method) as span:
      try:
        return handler(_OpenTracingServicerContext(servicer_context, span))
      except:
        e = sys.exc_info()[0]
        span.set_tag('error', True)
        span.log_kv({'event': 'error', 'error.object': e})
        raise

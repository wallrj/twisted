# -*- test-case-name: twisted.conch.test.test_endpoints -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Endpoint implementations of various SSH interactions.
"""

__all__ = [
    'ISSHConnectionCreator',

    'AuthenticationFailed', 'SSHCommandAddress', 'SSHCommandEndpoint']

from os.path import expanduser

from zope.interface import Interface, implementer

from twisted.python.filepath import FilePath
from twisted.python.failure import Failure
from twisted.internet.error import ConnectionDone
from twisted.internet.interfaces import IStreamClientEndpoint
from twisted.internet.protocol import Factory
from twisted.internet.defer import Deferred, succeed
from twisted.internet.endpoints import TCP4ClientEndpoint

from twisted.conch.ssh.keys import Key
from twisted.conch.ssh.common import NS
from twisted.conch.ssh.transport import SSHClientTransport
from twisted.conch.ssh.connection import SSHConnection
from twisted.conch.ssh.userauth import SSHUserAuthClient
from twisted.conch.ssh.channel import SSHChannel
from twisted.conch.client.knownhosts import KnownHostsFile
from twisted.conch.client.agent import SSHAgentClient


class AuthenticationFailed(Exception):
    """
    An SSH session could not be established because authentication was not
    successful.
    """



class ISSHConnectionCreator(Interface):
    """
    An L{ISSHConnectionCreator} knows how to create SSH connections somehow.
    """
    def secureConnection():
        """
        Return a new, connected, secured, but not yet authenticated instance of
        L{twisted.conch.ssh.transport.SSHServerTransport} or
        L{twisted.conch.ssh.transport.SSHClientTransport}.
        """


    def cleanupConnection(connection):
        """
        Perform cleanup necessary for a connection object previously returned
        from this creator's C{secureConnection} method.

        @param connection: An L{twisted.conch.ssh.transport.SSHServerTransport}
            or L{twisted.conch.ssh.transport.SSHClientTransport} returned by a
            previous call to C{secureConnection}.  It is no longer needed by the
            caller of that method and may be closed or otherwise cleaned up as
            necessary.
        """



class SSHCommandAddress(object):
    """
    An L{SSHCommandAddress} instance represents the address of an SSH server, a
    username which was used to authenticate with that server, and a command
    which was run there.

    @ivar server: See L{__init__}
    @ivar username: See L{__init__}
    @ivar command: See L{__init__}
    """
    def __init__(self, server, username, command):
        """
        @param server: The address of the SSH server on which the command is
            running.
        @type server: L{IAddress} provider

        @param username: An authentication username which was used to
            authenticate against the server at the given address.
        @type username: L{bytes}

        @param command: A command which was run in a session channel on the
            server at the given address.
        @type command: L{bytes}
        """
        self.server = server
        self.username = username
        self.command = command



class _CommandChannel(SSHChannel):
    """
    A L{_CommandChannel} executes a command in a session channel and connects
    its input and output to an L{IProtocol} provider.

    @ivar _creator: See L{__init__}
    @ivar _command: See L{__init__}
    @ivar _protocolFactory:  See L{__init__}
    @ivar _commandConnected:  See L{__init__}
    @ivar _protocol: An L{IProtocol} provider created using C{_protocolFactory}
        which is hooked up to the running command's input and output streams.
    """
    name = b'session'

    def __init__(self, creator, command, protocolFactory, commandConnected):
        """
        @param creator: The L{ISSHConnectionCreator} provider which was used to
            get the connection which this channel exists on.
        @type creator: L{ISSHConnectionCreator} provider

        @param command: The command to be executed.
        @type command: L{bytes}

        @param protocolFactory: A client factory to use to build a L{IProtocol}
            provider to use to associate with the running command.

        @param commandConnected:
        @type commandConnected: L{Deferred}
        """
        SSHChannel.__init__(self)
        self._creator = creator
        self._command = command
        self._protocolFactory = protocolFactory
        self._commandConnected = commandConnected


    def openFailed(self, reason):
        """
        When the request to open a new channel to run this command in fails,
        fire the C{commandConnected} deferred with a failure indicating that.
        """
        self._commandConnected.errback(reason)


    def channelOpen(self, ignored):
        """
        When the request to open a new channel to run this command in succeeds,
        issue an C{"exec"} request to run the command.
        """
        command = self.conn.sendRequest(
            self, 'exec', NS(self._command), wantReply=True)
        command.addCallbacks(self._execSuccess, self._execFailure)


    def _execFailure(self, reason):
        """
        When the request to execute the command in this channel fails, fire the
        C{commandConnected} deferred with a failure indicating this.
        """
        self._commandConnected.errback(reason)


    def _execSuccess(self, ignored):
        """
        When the request to execute the command in this channel succeeds, use
        C{protocolFactory} to build a protocol to handle the command's input and
        output and connect the protocol to a transport representing those
        streams.

        Also fire C{commandConnected} with the created protocol after it is
        connected to its transport.
        """
        self._protocol = self._protocolFactory.buildProtocol(
            SSHCommandAddress(
                self.conn.transport.transport.getPeer(),
                self.conn.transport.factory.username,
                self.conn.transport.factory.command))
        self._protocol.makeConnection(self)
        self._commandConnected.callback(self._protocol)


    def dataReceived(self, data):
        """
        When the command's stdout data arrives over the channel, deliver it to
        the protocol instance.

        @param data: The bytes from the command's stdout.
        @type data: L{bytes}
        """
        self._protocol.dataReceived(data)


    def closed(self):
        """
        When the channel closes, deliver disconnection notification to the
        protocol.
        """
        self._creator.cleanupConnection(self.conn)
        self._protocol.connectionLost(
            Failure(ConnectionDone("ssh channel closed")))



class _ConnectionReady(SSHConnection):
    """
    L{_ConnectionReady} is an L{twisted.conch.ssh.transport.service.SSHService}
    which only propagates the I{serviceStarted} event to a L{Deferred} to be
    handled elsewhere.
    """
    def __init__(self, factory):
        """
        @param factory: Some random object with a C{connectionReady} attribute
            which is a L{Deferred} which will get fired when I{serviceStarted}
            happens.  TODO This is a poor docstring.
        """
        SSHConnection.__init__(self)
        self._factory = factory


    def serviceStarted(self):
        """
        When the SSH I{connection} I{service} this object represents is ready to
        be used, fire the C{connectionReady} L{Deferred} to publish that event
        to some other interested party.

        Also clear the reference to that deferred on its container to help the
        garbage collector and to signal to other parts of this implementation
        that it has already been fired and does not need to be fired again.
        """
        d, self._factory.connectionReady = self._factory.connectionReady, None
        d.callback(self)



class UserAuth(SSHUserAuthClient):
    """
    L{UserAuth} implements the client part of SSH user authentication in the
    convenient way a user might expect if they are familiar with the interactive
    I{ssh} command line client.

    L{UserAuth} supports key-based authentication, password-based
    authentication, and delegating authentication to an agent.
    """
    password = None
    keys = None
    agent = None

    def getPublicKey(self):
        """
        Retrieve the next public key object to offer to the server, possibly
        delegating to an authentication agent if there is one.

        @return: The public part of a key pair that could be used to
            authenticate with the server, or C{None} if there are no more public
            keys to try.
        @rtype: L{twisted.conch.ssh.keys.Key} or L{NoneType}
        """
        if self.agent is not None:
            return self.agent.getPublicKey()

        if self.keys:
            self.key = self.keys.pop(0)
        else:
            self.key = None
        return self.key.public()


    def signData(self, publicKey, signData):
        """
        Extend the base signing behavior by using an SSH agent to sign the
        data, if one is available.

        @type publicKey: L{Key}
        @type signData: C{str}
        """
        if self.agent is not None:
            return self.agent.signData(publicKey.blob(), signData)
        else:
            return SSHUserAuthClient.signData(self, publicKey, signData)


    def getPrivateKey(self):
        """
        Get the private part of a key pair to use for authentication.  The key
        corresponds to the public part most recently returned from
        C{getPublicKey}.

        @return: A L{Deferred} which fires with the private key.
        @rtype: L{Deferred}
        """
        return succeed(self.key)


    def getPassword(self):
        """
        Get the password to use for authentication.

        @return: A L{Deferred} which fires with the password.
        """
        return succeed(self.password)


    def ssh_USERAUTH_SUCCESS(self, packet):
        """
        Handle user authentication success in the normal way, but also make a
        note of the state change on the L{_CommandTransport}.
        """
        self.transport._state = b'CHANNELLING'
        return SSHUserAuthClient.ssh_USERAUTH_SUCCESS(self, packet)



class _CommandTransport(SSHClientTransport):
    """
    L{_CommandTransport} is an SSH client I{transport} which includes a host key
    verification step before it will proceed to secure the connection.

    L{_CommandTransport} also knows how to set up a connection to an
    authentication agent if it is told where it can connect to one.
    """

    _state = b'STARTING' # -> b'SECURING' -> b'AUTHENTICATING' -> b'CHANNELLING' -> b'RUNNING'

    _hostKeyFailure = None

    def verifyHostKey(self, hostKey, fingerprint):
        """
        Verify the host key presented by the server by asking the L{IKnownHosts}
        provider available on the factory which created this protocol.

        @return: A L{Deferred} which fires with the result of
            L{IKnownHosts.verifyHostKey}.
        """
        hostname = self.factory.hostname
        ip = self.transport.getPeer().host

        self._state = b'SECURING'
        d = self.factory.knownHosts.verifyHostKey(
            self.factory.ui, hostname, ip, Key.fromString(hostKey))
        d.addErrback(self._saveHostKeyFailure)
        return d


    def _saveHostKeyFailure(self, reason):
        """
        When host key verification fails, record the reason for the failure in
        order to fire a L{Deferred} with it later.
        """
        self._hostKeyFailure = reason
        return reason


    def connectionSecure(self):
        """
        When the connection is secure, start the authentication process.
        """
        self._state = b'AUTHENTICATING'

        command = _ConnectionReady(self.factory)

        userauth = UserAuth(self.factory.username, command)
        userauth.password = self.factory.password
        if self.factory.keys:
            userauth.keys = list(self.factory.keys)

        if self.factory.agentEndpoint is not None:
            d = self._connectToAgent(userauth, self.factory.agentEndpoint)
        else:
            d = succeed(None)

        def maybeGotAgent(ignored):
            self.requestService(userauth)
        d.addBoth(maybeGotAgent)


    def _connectToAgent(self, userauth, endpoint):
        """
        Set up a connection to the authentication agent and trigger its
        initialization.

        @param userauth: The L{UserAuth} instance which is in charge of the
            overall authentication process.
        @type userauth: L{UserAuth}

        @param endpoint: An endpoint which can be used to connect to the
            authentication agent.
        @type endpoint: L{IStreamClientEndpoint} provider

        @return: A L{Deferred} which fires when the agent connection is ready
            for use.
        """
        factory = Factory()
        factory.protocol = SSHAgentClient
        d = endpoint.connect(factory)
        def connected(agent):
            userauth.agent = agent
            return agent.getPublicKeys()
        d.addCallback(connected)
        return d


    def connectionLost(self, reason):
        """
        When the underlying connection to the SSH server is lost, if there were
        any connection setup errors, propagate them.
        """
        if self._state == b'RUNNING' or self.factory.connectionReady is None:
            return
        if self._state == b'SECURING' and self._hostKeyFailure is not None:
            reason = self._hostKeyFailure
        elif self._state == b'AUTHENTICATING':
            reason = Failure(AuthenticationFailed("Doh"))
        self.factory.connectionReady.errback(reason)



@implementer(IStreamClientEndpoint)
class SSHCommandEndpoint(object):
    """
    L{SSHCommandEndpoint} exposes the command-executing functionality of SSH servers.

    L{SSHCommandEndpoint} can set up a new SSH, authenticate it in any one of a
    number of different ways (keys, passwords, agents), launch a command over
    that connection and then associate its input and output with a protocol.

    It can also re-use an existing, already-authenticated SSH connection
    (perhaps one which already has some SSH channels being used for other
    purposes).  In this case it creates a new SSH channel to use to execute the
    command.  Notably this means it supports multiplexing several different
    command invocations over a single SSH connection.
    """

    def __init__(self, creator, command):
        """
        @param creator: An L{ISSHConnectionCreator} provider which will be used
            to set up the SSH connection which will be used to run a command.
        @type creator: L{ISSHConnectionCreator} provider

        @param command: The command line to execute on the SSH server.  This
            byte string is interpreted by a shell on the SSH server, so it may
            have a value like C{"ls /"}.  Take care when trying to run a command
            like C{"/Volumes/My Stuff/a-program"} - spaces (and other special
            bytes) may require escaping.
        @type command: L{bytes}

        """
        self._creator = creator
        self._command = command


    @classmethod
    def newConnection(cls, reactor, command, username, hostname, port=None,
                      keys=None, password=None, agentEndpoint=None,
                      knownHosts=None, ui=None):
        """
        Create and return a new endpoint which will try to create a new
        connection to an SSH server and run a command over it.  It will also
        close the connection if there are problems leading up to the command
        being executed or after the command finishes.

        @param reactor: The reactor to use to establish the connection.
        @type reactor: L{IReactorTCP} provider

        @param command: See L{__init__}'s C{command} argument.

        @param username: The username with which to authenticate to the SSH
            server.
        @type username: L{bytes}

        @param hostname: The hostname of the SSH server.
        @type hostname: L{bytes}

        @param port: The port number of the SSH server.  By default, the
            standard SSH port number is used.
        @type port: L{int}

        @param keys: Private keys with which to authenticate to the SSH server,
            if key authentication is to be attempted (otherwise C{None}).
        @type keys: L{list} of L{Key}

        @param password: The password with which to authenticate to the SSH
            server, if password authentication is to be attempted (otherwise
            C{None}).
        @type password: L{bytes} or L{NoneType}

        @param agentEndpoint: An L{IStreamClientEndpoint} provider which may be
            used to connect to an SSH agent, if one is to be used to help with
            authentication.
        @type agentEndpoint: L{IStreamClientEndpoint} provider

        @param knownHosts: The currently known host keys, used to check the
            host key presented by the server we actually connect to.
        @type knownHosts: L{KnownHostsKey}

        @param ui: An object for interacting with users to make decisions about
            whether to accept the server host keys.
        @type ui: L{ConsoleUI}

        @return: A new instance of C{cls} (probably L{SSHCommandEndpoint}).
        """
        helper = _NewConnectionHelper(
            reactor, hostname, port, command, username, keys, password,
            agentEndpoint, knownHosts, ui)
        return cls(helper, command)


    @classmethod
    def existingConnection(cls, connection, command):
        """
        Create and return a new endpoint which will try to open a new channel on
        an existing SSH connection and run a command over it.  It will B{not}
        close the connection if there is a problem executing the command or
        after the command finishes.

        @param connection: An existing connection to an SSH server.
        @type connection: L{SSHConnection}

        @param command: See L{SSHCommandEndpoint.newConnection}'s C{command}
            parameter.
        @type command: L{bytes}

        @return: A new instance of C{cls} (probably L{SSHCommandEndpoint}).
        """
        helper = _ExistingConnectionHelper(connection)
        return cls(helper, command)


    def connect(self, protocolFactory):
        """
        Set up an SSH connection, use a channel from that connection to launch
        a command, and hook the stdin and stdout of that command up as a
        transport for a protocol created by the given factory.

        @param protocolFactory: A L{Factory} to use to create the protocol
            which will be connected to the stdin and stdout of the command on
            the SSH server.

        @return: A L{Deferred} which will fire with an error if the connection
            cannot be set up for any reason or with the protocol instance
            created by C{protocolFactory} once it has been connected to the
            command.
        """
        d = self._creator.secureConnection()
        d.addCallback(self._executeCommand, protocolFactory)
        return d


    def _executeCommand(self, connection, protocolFactory):
        """
        Given a secured SSH connection, try to execute a command in a new
        channel created on it and associate the result with a protocol from the
        given factory.

        @param connection: See L{SSHCommandEndpoint.existingConnection}'s
            C{connection} parameter.

        @param protocolFactory: See L{SSHCommandEndpoint.connect}'s
            C{protocolFactory} parameter.

        @return: See L{SSHCommandEndpoint.connect}'s return value.
        """
        commandConnected = Deferred()
        def disconnectOnFailure(passthrough):
            self._creator.cleanupConnection(connection)
            return passthrough
        commandConnected.addErrback(disconnectOnFailure)

        channel = _CommandChannel(
            self._creator, self._command, protocolFactory, commandConnected)
        channel._creator = self._creator
        connection.openChannel(channel)
        return commandConnected



@implementer(ISSHConnectionCreator)
class _NewConnectionHelper(object):
    """
    L{_NewConnectionHelper} implements L{ISSHConnectionCreator} by establishing
    a brand new SSH connection, securing it, and authenticating.
    """

    _KNOWN_HOSTS = "~/.ssh/known_hosts"

    def __init__(self, reactor, hostname, port, command, username, keys,
                 password, agentEndpoint, knownHosts, ui):
        self.reactor = reactor
        self.hostname = hostname
        self.port = port
        self.command = command
        self.username = username
        self.keys = keys
        self.password = password
        self.agentEndpoint = agentEndpoint
        if knownHosts is None:
            knownHosts = self._knownHosts()
        self.knownHosts = knownHosts
        self.ui = ui


    @classmethod
    def _knownHosts(cls):
        """
        Create and return a L{KnownHostsFile} instance pointed at the user's
        personal I{known hosts} file.
        """
        return KnownHostsFile.fromPath(FilePath(expanduser(cls._KNOWN_HOSTS)))


    def secureConnection(self):
        """
        Create and return a new SSH connection which has been secured and on
        which authentication has already happened.

        @return: A L{Deferred} which fires with the ready-to-use connection or
            with a failure if something prevents the connection from being
            setup, secured, or authenticated.
        """
        factory = Factory()
        factory.protocol = _CommandTransport
        factory.hostname = self.hostname
        factory.username = self.username
        factory.keys = self.keys
        factory.password = self.password
        factory.agentEndpoint = self.agentEndpoint
        factory.knownHosts = self.knownHosts
        factory.command = self.command
        factory.ui = self.ui
        factory.creator = self

        factory.connectionReady = Deferred()

        sshClient = TCP4ClientEndpoint(self.reactor, self.hostname, self.port)

        d = sshClient.connect(factory)
        d.addCallback(lambda ignored: factory.connectionReady)
        return d


    def cleanupConnection(self, connection):
        """
        Clean up the connection by closing it.  The command running on the
        endpoint has ended so the connection is no longer needed.
        """
        connection.transport.loseConnection()



@implementer(ISSHConnectionCreator)
class _ExistingConnectionHelper(object):
    """
    L{_ExistingConnectionHelper} implements L{ISSHConnectionCreator} by handing
    out an existing SSH connection which is supplied to its initializer.
    """

    def __init__(self, connection):
        """
        @param connection: See L{SSHCommandEndpoint.existingConnection}'s
            C{connection} parameter.
        """
        self.connection = connection


    def secureConnection(self):
        """
        @return: A L{Deferred} that fires synchronously with the
            already-established connection object.
        """
        return succeed(self.connection)


    def cleanupConnection(self, connection):
        """
        Do not do any cleanup on the connection.  Leave that responsibility to
        whatever code created it in the first place.
        """
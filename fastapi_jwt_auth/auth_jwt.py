import jwt, re, uuid
from jwt.algorithms import requires_cryptography, has_crypto
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Union, Sequence
from types import GeneratorType
from fastapi import Request
from fastapi_jwt_auth.auth_config import AuthConfig
from fastapi_jwt_auth.exceptions import (
    InvalidHeaderError,
    JWTDecodeError,
    RevokedTokenError,
    MissingHeaderError,
    AccessTokenRequired,
    RefreshTokenRequired,
    FreshTokenRequired
)

class AuthJWT(AuthConfig):
    def __init__(self,req: Request):
        """
        Get jwt from header or cookie (development) from an incoming request

        :param req: all incoming request
        :return: None
        """
        if req:
            auth = req.headers.get(self._header_name.lower())
            if auth: self._get_jwt_from_headers(auth)

    def _get_jwt_from_headers(self,auth: str) -> "AuthJWT":
        """
        Get token from the header

        :param auth: value from HeaderName
        :return: None
        """
        header_name = self._header_name
        header_type = self._header_type

        parts = auth.split()

        # Make sure the header is in a valid format that we are expecting, ie
        if not header_type:
            # <HeaderName>: <JWT>
            if len(parts) != 1:
                msg = "Bad {} header. Expected value '<JWT>'".format(header_name)
                raise InvalidHeaderError(status_code=422,message=msg)
            self._token = parts[0]
        else:
            # <HeaderName>: <HeaderType> <JWT>
            if not re.match(r"{}\s".format(header_type),auth) or len(parts) != 2:
                msg = "Bad {} header. Expected value '{} <JWT>'".format(header_name,header_type)
                raise InvalidHeaderError(status_code=422,message=msg)
            self._token = parts[1]

    def _get_jwt_identifier(self) -> str:
        return str(uuid.uuid4())

    def _get_int_from_datetime(self,value: datetime) -> int:
        """
        :param value: datetime with or without timezone, if don't contains timezone
                      it will managed as it is UTC
        :return: Seconds since the Epoch
        """
        if not isinstance(value, datetime):  # pragma: no cover
            raise TypeError('a datetime is required')
        return int(value.timestamp())

    def _get_secret_key(self, algorithm: str, process: str) -> str:
        """
        Get key with a different algorithm

        :param algorithm: algorithm for decode and encode token
        :param process: for indicating get key for encode or decode token

        :return: plain text or RSA depends on algorithm
        """
        symmetric_algorithms = {"HS256","HS384","HS512"}
        asymmetric_algorithms = requires_cryptography

        if algorithm not in symmetric_algorithms and algorithm not in asymmetric_algorithms:
            raise ValueError("Algorithm {} could not be found".format(algorithm))

        if algorithm in symmetric_algorithms:
            if not self._secret_key:
                raise RuntimeError(
                    "AUTHJWT_SECRET_KEY must be set when using symmetric algorithm {}".format(algorithm)
                )

            return self._secret_key

        if algorithm in asymmetric_algorithms and not has_crypto:
            raise RuntimeError(
                "Missing dependencies for using asymmetric algorithms. run 'pip install fastapi-jwt-auth[asymmetric]'"
            )

        if process == "encode":
            if not self._private_key:
                raise RuntimeError(
                    "AUTHJWT_PRIVATE_KEY must be set when using asymmetric algorithm {}".format(algorithm)
                )

            return self._private_key

        if process == "decode":
            if not self._public_key:
                raise RuntimeError(
                    "AUTHJWT_PUBLIC_KEY must be set when using asymmetric algorithm {}".format(algorithm)
                )

            return self._public_key

    def _create_token(
        self,
        identity: Union[str,int],
        type_token: str,
        exp_time: Optional[int],
        fresh: Optional[bool] = False,
        algorithm: Optional[str] = None,
        headers: Optional[Dict] = None,
        issuer: Optional[str] = None,
        audience: Optional[Union[str,Sequence[str]]] = None
    ) -> str:
        """
        This function create token for access_token and refresh_token, when type_token
        is access add a fresh key to dictionary payload

        :param identity: Identifier for who this token is for example id or username from database.
        :param type_token: for indicate token is access_token or refresh_token
        :param exp_time: Set the duration of the JWT
        :param fresh: Optional when token is access_token this param required
        :param algorithm: algorithm allowed to encode the token
        :param headers: valid dict for specifying additional headers in JWT header section
        :param issuer: expected issuer in the JWT
        :param audience: expected audience in the JWT

        :return: Encoded token
        """
        # Validation type data
        if not isinstance(identity, (str,int)):
            raise TypeError("identity must be a string or integer")
        if not isinstance(fresh, (bool)):
            raise TypeError("fresh must be a boolean")
        if audience and not isinstance(audience, (str, list, tuple, set, frozenset, GeneratorType)):
            raise TypeError("audience must be a string or sequence")
        if algorithm and not isinstance(algorithm, str):
            raise TypeError("algorithm must be a string")

        # Data section
        reserved_claims = {
            "iat": self._get_int_from_datetime(datetime.now(timezone.utc)),
            "nbf": self._get_int_from_datetime(datetime.now(timezone.utc)),
            "jti": self._get_jwt_identifier(),
        }

        custom_claims = {
            "identity": identity,
            "type": type_token
        }

        # for access_token only fresh needed
        if type_token == 'access':
            custom_claims['fresh'] = fresh

        if exp_time:
            reserved_claims['exp'] = exp_time
        if issuer:
            reserved_claims['iss'] = issuer
        if audience:
            reserved_claims['aud'] = audience

        algorithm = algorithm or self._algorithm

        try:
            secret_key = self._get_secret_key(algorithm,"encode")
        except Exception:
            raise

        return jwt.encode(
            {**reserved_claims, **custom_claims},
            secret_key,
            algorithm=algorithm,
            headers=headers
        ).decode('utf-8')

    def _verifying_token(self,encoded_token: str, issuer: Optional[str] = None) -> None:
        """
        Verified token and check if token is revoked

        :param encoded_token: token hash
        :param issuer: expected issuer in the JWT
        :return: None
        """
        raw_token = self._verified_token(encoded_token=encoded_token,issuer=issuer)
        if raw_token['type'] in self._denylist_token_checks:
            self._check_token_is_revoked(raw_token)

    def _verified_token(self,encoded_token: str, issuer: Optional[str] = None) -> Dict[str,Union[str,int,bool]]:
        """
        Verified token and catch all error from jwt package and return decode token

        :param encoded_token: token hash
        :param issuer: expected issuer in the JWT
        :return: raw data from the hash token in the form of a dictionary
        """
        algorithms = self._decode_algorithms or [self._algorithm]

        try:
            unverified_headers = self.get_unverified_jwt_headers(encoded_token)
        except Exception as err:
            raise InvalidHeaderError(status_code=422,message=str(err))

        try:
            secret_key = self._get_secret_key(unverified_headers['alg'],"decode")
        except Exception:
            raise

        try:
            return jwt.decode(
                encoded_token,
                secret_key,
                issuer=issuer,
                audience=self._decode_audience,
                leeway=self._decode_leeway,
                algorithms=algorithms
            )
        except Exception as err:
            raise JWTDecodeError(status_code=422,message=str(err))

    def _has_token_in_denylist_callback(self) -> bool:
        """
        Return True if token denylist callback set
        """
        return self._token_in_denylist_callback is not None

    def _check_token_is_revoked(self, raw_token: Dict[str,Union[str,int,bool]]) -> None:
        """
        Ensure that AUTHJWT_DENYLIST_ENABLED is true and callback regulated, and then
        call function denylist callback with passing decode JWT, if true
        raise exception Token has been revoked
        """
        if not self._denylist_enabled:
            return

        if not self._has_token_in_denylist_callback():
            raise RuntimeError("A token_in_denylist_callback must be provided via "
                "the '@AuthJWT.token_in_denylist_loader' if "
                "AUTHJWT_DENYLIST_ENABLED is 'True'")

        if self._token_in_denylist_callback.__func__(raw_token):
            raise RevokedTokenError(status_code=401,message="Token has been revoked")

    def _get_expired_time(
        self,
        type_token: str,
        expires_time: Optional[Union[timedelta,int,bool]] = None
    ) -> Union[None,int]:
        """
        Dynamic token expired if expires_time is False exp claim not created

        :param type_token: for indicate token is access_token or refresh_token
        :param expires_time: duration expired jwt

        :return: duration exp claim jwt
        """
        if expires_time and not isinstance(expires_time, (timedelta,int,bool)):
            raise TypeError("expires_time must be between timedelta, int, bool")

        if expires_time is not False:
            if type_token == 'access':
                expires_time = expires_time or self._access_token_expires
            if type_token == 'refresh':
                expires_time = expires_time or self._refresh_token_expires

        if expires_time is not False:
            if isinstance(expires_time, bool):
                if type_token == 'access':
                    expires_time = self._access_token_expires
                if type_token == 'refresh':
                    expires_time = self._refresh_token_expires
            if isinstance(expires_time, timedelta):
                expires_time = int(expires_time.total_seconds())

            return self._get_int_from_datetime(datetime.now(timezone.utc)) + expires_time
        else:
            return None

    def create_access_token(
        self,
        identity: Union[str,int],
        fresh: Optional[bool] = False,
        algorithm: Optional[str] = None,
        headers: Optional[Dict] = None,
        expires_time: Optional[Union[timedelta,int,bool]] = None,
        audience: Optional[Union[str,Sequence[str]]] = None
    ) -> str:
        """
        Create a access token with 15 minutes for expired time (default),
        info for param and return please check to function create token

        :return: hash token
        """
        return self._create_token(
            identity=identity,
            type_token="access",
            exp_time=self._get_expired_time("access",expires_time),
            fresh=fresh,
            algorithm=algorithm,
            headers=headers,
            audience=audience,
            issuer=self._encode_issuer
        )

    def create_refresh_token(
        self,
        identity: Union[str,int],
        algorithm: Optional[str] = None,
        headers: Optional[Dict] = None,
        expires_time: Optional[Union[timedelta,int,bool]] = None,
        audience: Optional[Union[str,Sequence[str]]] = None
    ) -> str:
        """
        Create a refresh token with 30 days for expired time (default),
        info for param and return please check to function create token

        :return: hash token
        """
        return self._create_token(
            identity=identity,
            type_token="refresh",
            exp_time=self._get_expired_time("refresh",expires_time),
            algorithm=algorithm,
            headers=headers,
            audience=audience
        )

    def jwt_required(self) -> None:
        """
        Only access token can access this function

        :return: None
        """
        if self._token:
            self._verifying_token(encoded_token=self._token,issuer=self._decode_issuer)

        if not self._token:
            raise MissingHeaderError(status_code=401,message="Missing {} Header".format(self._header_name))

        if self._get_type_token() != 'access':
            raise AccessTokenRequired(status_code=422,message="Only access tokens are allowed")

    def jwt_optional(self) -> None:
        """
        If an access token in present in the request you can get data from get_raw_jwt() or get_jwt_identity(),
        If no access token is present in the request, this endpoint will still be called, but
        get_raw_jwt() or get_jwt_identity() will return None

        :return: None
        """
        if self._token:
            self._verifying_token(encoded_token=self._token,issuer=self._decode_issuer)

        if self._token and self._get_type_token() != 'access':
            raise AccessTokenRequired(status_code=422,message="Only access tokens are allowed")

    def jwt_refresh_token_required(self) -> None:
        """
        This function will ensure that the requester has a valid refresh token

        :return: None
        """
        if self._token:
            self._verifying_token(encoded_token=self._token)

        if not self._token:
            raise MissingHeaderError(status_code=401,message="Missing {} Header".format(self._header_name))

        if self._get_type_token() != 'refresh':
            raise RefreshTokenRequired(status_code=422,message="Only refresh tokens are allowed")

    def fresh_jwt_required(self) -> None:
        """
        This function will ensure that the requester has a valid and fresh access token

        :return: None
        """
        if self._token:
            self._verifying_token(encoded_token=self._token,issuer=self._decode_issuer)

        if not self._token:
            raise MissingHeaderError(status_code=401,message="Missing {} Header".format(self._header_name))

        if self._get_type_token() != 'access':
            raise AccessTokenRequired(status_code=422,message="Only access tokens are allowed")

        if not self._get_fresh_token():
            raise FreshTokenRequired(status_code=401,message="Fresh token required")

    def _get_type_token(self) -> str:
        return self.get_raw_jwt()['type']

    def _get_fresh_token(self) -> bool:
        return self.get_raw_jwt()['fresh']

    def get_raw_jwt(self) -> Optional[Dict[str,Union[str,int,bool]]]:
        """
        this will return the python dictionary which has all of the claims of the JWT that is accessing the endpoint.
        If no JWT is currently present, return None instead

        :return: claims of JWT
        """
        if self._token:
            return self._verified_token(encoded_token=self._token)
        return None

    def get_jti(self,encoded_token: str) -> str:
        """
        Returns the JTI (unique identifier) of an encoded JWT

        :return: string of JTI
        """
        return self._verified_token(encoded_token=encoded_token)['jti']

    def get_jwt_identity(self) -> Optional[Union[str,int]]:
        """
        this will return the identity of the JWT that is accessing this endpoint.
        If no JWT is present, `None` is returned instead.

        :return: identity of JWT
        """
        if self._token:
            return self._verified_token(encoded_token=self._token)['identity']
        return None

    def get_unverified_jwt_headers(self,encoded_token: Optional[str] = None) -> dict:
        """
        Returns the Headers of an encoded JWT without verifying the actual signature of JWT

        :param encoded_token: The encoded JWT to get the Header from
        :return: JWT header parameters as a dictionary
        """
        encoded_token = encoded_token or self._token

        return jwt.get_unverified_header(encoded_token)

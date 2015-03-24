//
// INTEL CONFIDENTIAL
//
// Copyright 2013-2014 Intel Corporation All Rights Reserved.
//
// The source code contained or described herein and all documents related
// to the source code ("Material") are owned by Intel Corporation or its
// suppliers or licensors. Title to the Material remains with Intel Corporation
// or its suppliers and licensors. The Material contains trade secrets and
// proprietary and confidential information of Intel or its suppliers and
// licensors. The Material is protected by worldwide copyright and trade secret
// laws and treaty provisions. No part of the Material may be used, copied,
// reproduced, modified, published, uploaded, posted, transmitted, distributed,
// or disclosed in any way without Intel's prior express written permission.
//
// No license under any patent, copyright, trade secret or other intellectual
// property right is granted to or conferred upon you by disclosure or delivery
// of the Materials, either expressly, by implication, inducement, estoppel or
// otherwise. Any license under such intellectual property rights must be
// express and approved by Intel in writing.

'use strict';

exports.wiretree = function getSessionFactory (requestStream, renderRequestError) {
  /**
   * Uses the passed in cookie to get a corresponding session.
   * @param {Object} req
   * @param {Object} res
   * @param {Function} next
   */
  return function getSession (req, res, next) {
    var cookie = req.clientReq.headers.cookie || '';

    requestStream('/session', {
      headers: { cookie: cookie }
    })
      .stopOnError(renderRequestError(res, function writeDescription (err) {
        return 'Exception rendering resources: ' + err.stack;
      }))
      .each(function setData (response) {
        // Pass the session cookies to the client.
        res.clientRes.setHeader('Set-Cookie', response.headers['set-cookie']);

        var data = {
          session: response.body,
          cacheCookie: response.headers['set-cookie']
            .map(function extractAuthCookies (cookieString) {
              return cookieString.match(/((?:csrftoken|sessionid)=[^;]+;)/)[0];
            }).join(' ')
        };

        next(req, res, data);
      });
  };
};

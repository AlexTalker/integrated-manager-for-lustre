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

exports.wiretree = function getDirTreeSyncFactory (fs, path) {
  /**
   * Recursively builds a directory tree
   * And returns a flat object.
   * @param {String} dir
   * @param {Function} transformPath
   * @returns {Object}
   */
  return function getDirTreeSync (dir, transformPath, dirTree) {
    var files = fs.readdirSync(dir);

    return files
      .reduce(function buildTree (obj, file) {
        var filePath = path.join(dir, file);

        var s = fs.statSync(filePath);

        if (s.isFile() && /\.html$/.test(filePath))
          obj[transformPath(filePath)] = fs.readFileSync(filePath, 'utf8');
        if (s.isDirectory())
          getDirTreeSync(filePath + '/', transformPath, obj);

        return obj;
      }, dirTree || {});
  };
};
